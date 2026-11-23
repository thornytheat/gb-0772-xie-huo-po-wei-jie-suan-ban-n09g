from bisect import bisect_left
from collections import Counter, defaultdict
from copy import deepcopy

from app.archive.canonical import digest
from app.curb.timeaxis import M, count, minute, span
from app.problems import DocketError

BATCH_DOCUMENT='泊位理赔证据'
VEHICLE_DOCUMENT='本车泊位理赔证据'
DISPUTE_CODES=frozenset({'PLATE_OVERLAP','BERTH_OVERLAP'})
BILLED_CODES=('FREE','BASE','UPGRADE')
KNOWN_CODES=DISPUTE_CODES|frozenset(BILLED_CODES)


def _check_berths(account):
    for row in account['berths']:
        remainder=row['scheduled']-sum(row[key] for key in ('vacant','disputed','free','base','upgrade'))
        if remainder:
            raise DocketError('BERTH_UNBALANCED','泊位分钟勾稽未平',409)


def _check_money(account):
    money=account['money']
    if money['paid_cents']-money['due_cents']!=money['difference_cents'] or any(
        r['paid_cents']-r['due_cents']!=r['difference_cents'] for r in account['lines']
    ):
        raise DocketError('MONEY_UNBALANCED','收费差价勾稽未平',409)


def _check_batch(evidence):
    account=evidence['settlement']
    _check_berths(account)
    _check_money(account)


def issue(packet):
    evidence=packet['evidence']
    if digest(evidence)!=packet['manifest']['evidence_sha256']:
        raise DocketError('DIGEST_BAD','证据内容与留档摘要不同',409)
    _check_batch(evidence)
    return {'document':BATCH_DOCUMENT,'minutes_balance':0,'sha256':digest(packet),**packet}


def _sealed(record):
    if not isinstance(record,dict):
        raise DocketError('DOCKET_BAD','留档结构不完整',422)
    manifest,evidence=record.get('manifest'),record.get('evidence')
    if not isinstance(manifest,dict) or not isinstance(evidence,dict):
        raise DocketError('DOCKET_BAD','留档结构不完整',422)
    try:
        expected=manifest['evidence_sha256']
        manifest['key']
        evidence['occupancy']['stays']
        evidence['pricing']['stays']
        evidence['settlement']
    except (KeyError,TypeError):
        raise DocketError('DOCKET_BAD','留档结构不完整',422) from None
    if digest(evidence)!=expected:
        raise DocketError('DIGEST_BAD','证据内容与留档摘要不同',409)
    signed=record.get('sha256')
    if signed is not None and signed!=digest({'manifest':manifest,'evidence':evidence}):
        raise DocketError('DIGEST_BAD','证据根摘要与内容不同',409)
    return manifest,evidence


def _time(raw):
    try:
        return minute(raw)
    except (TypeError,ValueError):
        raise DocketError('DOCKET_BAD','留档证据含无效时刻',409) from None


def _resolve(evidence,reference):
    '''受理车辆代号，或该车任一次停靠编号。'''
    if not isinstance(reference,str) or not reference:
        raise DocketError('VEHICLE_BAD','需指定本车车辆代号',422)
    stays=evidence['occupancy']['stays']
    if any(row.get('vehicle')==reference for row in stays):
        return reference
    owners={row['vehicle'] for row in stays if row.get('stay')==reference}
    if len(owners)==1:
        return owners.pop()
    raise DocketError('VEHICLE_MISSING','清算留档没有本车停靠',404)


def _runs(index):
    '''把争议分钟按泊位压成同原因的连续区间，附带起点表供二分。'''
    ordered=defaultdict(list)
    for (berth,when),code in sorted(index.items()):
        ordered[berth].append((when,code))
    runs={}
    for berth,items in ordered.items():
        spans,run=[],None
        for when,code in items:
            if run and run['code']==code and run['end']==when:
                run['end']=when+M
            else:
                run={'code':code,'start':when,'end':when+M}
                spans.append(run)
        runs[berth]=(tuple(run['start'] for run in spans),tuple(spans))
    return runs


def _intersecting_runs(runs,start,end):
    '''区间按起点排序且互不重叠：从 end 往前取，遇到已结束的区间即停。'''
    if not runs:
        return ()
    starts,spans=runs
    found=[]
    for index in range(bisect_left(starts,end)-1,-1,-1):
        if spans[index]['end']<=start:
            break
        found.append(spans[index])
    return found


def _basis(evidence):
    '''整批索引：只依赖留档，按请求建一次即可供各车复用。'''
    lineup={row['stay']:row for row in evidence['occupancy']['stays']}
    spans={stay:(row['berth'],_time(row['start']),_time(row['end'])) for stay,row in lineup.items()}
    berth_starts,vehicle_starts=defaultdict(list),defaultdict(list)
    for stay,(berth,start,end) in spans.items():
        berth_starts[berth].append((start,end,stay))
        vehicle_starts[lineup[stay]['vehicle']].append((start,end,stay))
    for rows in (*berth_starts.values(),*vehicle_starts.values()):
        rows.sort()
    index={(row['berth'],_time(row['at'])):row['code'] for row in evidence['occupancy']['disputes']}
    by_vehicle={'stays':{},'prices':{},'lines':{},'orphans':{},'isolated':{}}
    for section,rows in (('stays',evidence['occupancy']['stays']),
                         ('prices',evidence['pricing']['stays']),
                         ('lines',evidence['settlement']['lines']),
                         ('orphans',evidence['settlement']['orphans']),
                         ('isolated',evidence['settlement']['isolated'])):
        for row in rows:
            by_vehicle[section].setdefault(row.get('vehicle'),[]).append(row)
    return {'lineup':lineup,'spans':spans,'berth_starts':berth_starts,
            'vehicle_starts':vehicle_starts,'runs':_runs(index),'by_vehicle':by_vehicle}


def _overlapping(rows,start,end):
    '''已按 (start,end,stay) 排序：二分跳过起点不早于 end 的项，再滤掉已结束的。'''
    stop=bisect_left(rows,(end,))
    return (rows[index] for index in range(stop) if rows[index][1]>start)


def _dispute_proof(basis,vehicle,stops):
    '''逐分钟定位本车争议，并为每个争议区间给出重叠方摘录。

    摘录只含泊位、区间、分钟数、同车标记和整条留档记录的摘要；他车记录本身不外发。
    收件人可逐分钟求和得到本车争议时长，并用摘要与整批证据或对方证书对照。
    '''
    lineup,spans=basis['lineup'],basis['spans']
    runs,berth_starts,vehicle_starts=basis['runs'],basis['berth_starts'],basis['vehicle_starts']
    own=[]
    counterparts={}
    covered=defaultdict(set)
    for stop in stops:
        berth,start,end=spans[stop['stay']]
        for run in _intersecting_runs(runs.get(berth),start,end):
            a,b=max(start,run['start']),min(end,run['end'])
            if a>=b:
                continue
            record=run['code']
            for when in span(a,b):
                own.append({'berth':berth,'at':when.isoformat(),'code':record,'stay':stop['stay']})
                covered[berth,record].add(when)
            if record=='PLATE_OVERLAP':
                peers=_overlapping(berth_starts[berth],a,b)
                same_vehicle=False
            else:
                peers=_overlapping(vehicle_starts[vehicle],a,b)
                same_vehicle=True
            found=0
            for other_start,other_end,stay in peers:
                if stay==stop['stay']:
                    continue
                if same_vehicle and spans[stay][0]==berth:
                    continue
                group=(berth,run['code'],run['start'],stay)
                if group not in counterparts:
                    other=lineup[stay]
                    counterparts[group]={'berth':berth,'counterpart_berth':other['berth'],
                                         'code':record,'start':max(a,other_start),'end':min(b,other_end),
                                         'minutes':0,'same_vehicle':same_vehicle,
                                         'record_sha256':digest(other)}
                    if same_vehicle:
                        counterparts[group]['stay']=stay
                found+=1
            if not found:
                raise DocketError('EVIDENCE_MISMATCH','争议分钟缺少重叠方记录',409)
    for group,row in counterparts.items():
        row['minutes']=count(row['start'],row['end'])
    own_records={digest(stop) for stop in stops}
    witness=sorted((row|{'start':row['start'].isoformat(),'end':row['end'].isoformat()}
                    for row in counterparts.values()),
                   key=lambda row:(row['code'],row['berth'],row['start'],row.get('stay','')))
    witnessed=defaultdict(set)
    for row in witness:
        if row['minutes']<=0:
            raise DocketError('DISPUTE_UNBALANCED','重叠摘录区间倒置',409)
        seen=set(span(_time(row['start']),_time(row['end'])))
        if not seen<=covered[row['berth'],row['code']]:
            raise DocketError('DISPUTE_UNBALANCED','重叠摘录超出本车争议分钟',409)
        witnessed[row['berth'],row['code']]|=seen
        if row['same_vehicle'] and row['record_sha256']!=digest(lineup[row['stay']]):
            raise DocketError('EVIDENCE_MISMATCH','本车跨泊位摘录与停靠不符',409)
        if not row['same_vehicle'] and row['record_sha256'] in own_records:
            raise DocketError('EVIDENCE_MISMATCH','他车摘录与本车停靠重复',409)
    for key,minutes in covered.items():
        if minutes-witnessed[key]:
            raise DocketError('DISPUTE_UNBALANCED','本车争议分钟缺少重叠方凭据',409)
    return own,witness


def _vehicle_view(manifest,base,vehicle,basis,batch_sha):
    by_vehicle=basis['by_vehicle']
    stays=deepcopy(by_vehicle['stays'].get(vehicle,[]))
    prices=deepcopy(by_vehicle['prices'].get(vehicle,[]))
    lines=deepcopy(by_vehicle['lines'].get(vehicle,[]))
    if not stays or not prices:
        raise DocketError('VEHICLE_MISSING','清算留档没有本车停靠',404)
    berths=manifest.get('survey',{}).get('berths') if isinstance(manifest.get('survey'),dict) else None
    if not isinstance(berths,(list,tuple)):
        raise DocketError('DOCKET_BAD','留档缺少清算窗口泊位',422)
    order=[berth for berth in berths if any(row['berth']==berth for row in stays)]
    if any(row['berth'] not in order for row in stays):
        raise DocketError('EVIDENCE_MISMATCH','本车泊位不在清算窗口内',409)

    for row in prices:
        if sum(segment['minutes'] for segment in row['segments'])!=row['minutes']:
            raise DocketError('STAY_UNBALANCED','本车停靠分钟与分段不符',409)
        if any(segment['code'] not in KNOWN_CODES for segment in row['segments']):
            raise DocketError('STAY_UNBALANCED','本车分段出现未知费率',409)
        if any(segment['cents']!=segment['rate_cents']*segment['minutes'] for segment in row['segments']):
            raise DocketError('MONEY_UNBALANCED','本车分段金额与费率不符',409)
        if row['due_cents']!=sum(segment['cents'] for segment in row['segments']):
            raise DocketError('MONEY_UNBALANCED','本车停靠应收与分段不符',409)
    if len(prices)!=len(stays) or {row['stay'] for row in prices}!={row['stay'] for row in stays}:
        raise DocketError('EVIDENCE_MISMATCH','本车停靠与核价行不一致',409)
    stops={row['stay']:row for row in stays}
    for row in prices:
        source=stops[row['stay']]
        if (row['berth'],row['start'],row['end'],row['minutes'])!=(
                source['berth'],source['start'],source['end'],source['minutes']):
            raise DocketError('EVIDENCE_MISMATCH','本车核价行与停靠区间不一致',409)

    own,witness=_dispute_proof(basis,vehicle,stays)
    proved=Counter((row['stay'],row['code']) for row in own)
    for row in prices:
        charged=Counter(segment['code'] for segment in row['segments'] for _ in range(segment['minutes'])
                        if segment['code'] in DISPUTE_CODES)
        actual=Counter({code:count for (stay,code),count in proved.items() if stay==row['stay']})
        if charged!=actual:
            raise DocketError('DISPUTE_UNBALANCED','本车争议分钟与本车凭据不符',409)
    own_minutes=Counter(row['stay'] for row in own)

    occupied,disputed,categories=Counter(),Counter(),Counter()
    for row in prices:
        occupied[row['berth']]+=row['minutes']
        for segment in row['segments']:
            if segment['code'] in BILLED_CODES:
                categories[row['berth'],segment['code']]+=segment['minutes']
            else:
                disputed[row['berth']]+=segment['minutes']
    berths=[]
    for berth in order:
        row={'berth':berth,'occupied':occupied[berth],'disputed':disputed[berth],
             'free':categories[berth,'FREE'],'base':categories[berth,'BASE'],'upgrade':categories[berth,'UPGRADE']}
        if row['occupied']!=row['disputed']+row['free']+row['base']+row['upgrade']:
            raise DocketError('BERTH_UNBALANCED','本车泊位分钟勾稽未平',409)
        berths.append(row)
    minutes=[{'stay':row['stay'],'berth':row['berth'],'occupied':row['minutes'],
              'disputed':own_minutes[row['stay']],'billed':row['minutes']-own_minutes[row['stay']]}
             for row in stays]

    priced={row['stay']:row for row in prices}
    if len(lines)!=len(prices) or {row['stay'] for row in lines}!=set(priced):
        raise DocketError('EVIDENCE_MISMATCH','本车停靠与对账行不一致',409)
    for row in lines:
        if row['difference_cents']!=row['paid_cents']-row['due_cents']:
            raise DocketError('MONEY_UNBALANCED','本车收费差价勾稽未平',409)
        if row['paid_cents']!=sum(receipt['cents'] for receipt in row['receipts']):
            raise DocketError('MONEY_UNBALANCED','本车实收与回执合计不符',409)
        source=priced[row['stay']]
        if (row['berth'],row['arrival'],row['due_cents'],row['vehicle'])!=(
                source['berth'],source['start'],source['due_cents'],vehicle):
            raise DocketError('EVIDENCE_MISMATCH','本车对账行与停靠不一致',409)
    due=sum(row['due_cents'] for row in lines)
    paid=sum(row['paid_cents'] for row in lines)
    difference=sum(row['difference_cents'] for row in lines)
    if difference!=paid-due:
        raise DocketError('MONEY_UNBALANCED','本车收费差价勾稽未平',409)

    orphans=deepcopy(by_vehicle['orphans'].get(vehicle,[]))
    isolated=deepcopy(by_vehicle['isolated'].get(vehicle,[]))
    money={'due_cents':due,'paid_cents':paid,'difference_cents':difference,
           'orphan_cents':sum(row['cents'] for row in orphans)}

    starts,ends=[_time(row['start']) for row in stays],[_time(row['end']) for row in stays]
    scope={'vehicle':vehicle,'berths':[row['berth'] for row in berths],
           'start':min(starts).isoformat(),'end':max(ends).isoformat()}
    reconciliation={
        'batch':{'key':manifest['key'],'rule':manifest.get('rule'),'source':manifest.get('source'),
                 'created_utc':manifest.get('created_utc'),'evidence_sha256':manifest['evidence_sha256'],
                 'sha256':batch_sha},
        'occupied_minutes':sum(row['occupied'] for row in berths),
        'disputed_minutes':sum(row['disputed'] for row in berths),
        'disputed_by_berth':{row['berth']:row['disputed'] for row in berths},
        'due_cents':due,'paid_cents':paid,'difference_cents':difference,
        'receipts':[receipt['fields']['receipt'] for row in lines for receipt in row['receipts']],
        'stay_sha256':{row['stay']:digest(row) for row in stays},
        'line_sha256':{row['stay']:digest(row) for row in lines},
    }
    return scope,{
        'scope':scope,
        'occupancy':{'stays':stays,'disputes':own,'counterparts':witness,'minutes':minutes,
                     'removed_overlap_minutes':sum(row['observed_minutes']-row['minutes'] for row in stays)},
        'pricing':{'stays':prices,'due_cents':sum(row['due_cents'] for row in prices)},
        'settlement':{'lines':lines,'money':money,'berths':berths,'orphans':orphans,'isolated':isolated},
        'reconciliation':reconciliation,
    }


def _appeal_id(manifest,vehicle):
    return digest({'appeal':'vehicle','key':manifest['key'],'vehicle':vehicle})[:20]


def _package(manifest,evidence,vehicle,scope,view,batch_sha):
    appeal=_appeal_id(manifest,vehicle)
    receipt={'key':manifest['key'],'source':manifest.get('source'),'rule':manifest.get('rule'),
             'inputs':manifest.get('inputs'),'survey':manifest.get('survey'),
             'created_utc':manifest.get('created_utc'),'appeal':appeal,'vehicle':vehicle,'scope':scope,
             'evidence_sha256':digest(view),'batch_evidence_sha256':manifest['evidence_sha256'],
             'batch_sha256':batch_sha}
    return {'document':VEHICLE_DOCUMENT,'appeal':appeal,'key':manifest['key'],'vehicle':vehicle,
            'minutes_balance':0,'sha256':digest({'manifest':receipt,'evidence':view}),
            'manifest':receipt,'evidence':view}


def issue_vehicle(record,reference,basis=None):
    '''从已签的整批留档裁剪出本车证据；纯函数，重复调用逐字节相同，不写库。'''
    manifest,evidence=_sealed(record)
    _check_batch(evidence)
    vehicle=_resolve(evidence,reference)
    batch_sha=digest({'manifest':manifest,'evidence':evidence})
    scope,view=_vehicle_view(manifest,evidence,vehicle,basis or _basis(evidence),batch_sha)
    return _package(manifest,evidence,vehicle,scope,view,batch_sha)


def claims(record):
    '''列出留档中各车可签发的本车结论，供理赔页面核对并下载。'''
    manifest,evidence=_sealed(record)
    _check_batch(evidence)
    basis=_basis(evidence)
    batch_sha=digest({'manifest':manifest,'evidence':evidence})
    listed=[]
    for vehicle in sorted(basis['by_vehicle']['stays']):
        try:
            scope,view=_vehicle_view(manifest,evidence,vehicle,basis,batch_sha)
            listed.append({'vehicle':vehicle,'appeal':_appeal_id(manifest,vehicle),'scope':scope,
                           'stays':len(view['occupancy']['stays']),
                           'occupied':view['reconciliation']['occupied_minutes'],
                           'disputed':view['reconciliation']['disputed_minutes'],
                           'receipts':view['reconciliation']['receipts'],
                           'due_cents':view['settlement']['money']['due_cents'],
                           'paid_cents':view['settlement']['money']['paid_cents'],
                           'difference_cents':view['settlement']['money']['difference_cents'],'ok':True})
        except DocketError as error:
            listed.append({'vehicle':vehicle,'ok':False,'code':error.code,'message':str(error)})
    return listed
