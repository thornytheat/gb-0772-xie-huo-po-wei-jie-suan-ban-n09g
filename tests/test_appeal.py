from copy import deepcopy

import pytest

from app.archive.appeal import claims, issue, issue_vehicle
from app.archive.canonical import digest
from app.problems import DocketError


@pytest.fixture
def exported(client):
    key=client.post('/api/dockets',json={'sample':'dirty'}).json()['key']
    return client.get('/api/export/'+key)


@pytest.fixture
def sealed(client):
    key=client.post('/api/dockets',json={'sample':'dirty'}).json()['key']
    return key,client.get('/api/dockets/'+key).json()


@pytest.fixture
def sealed_clean(client):
    key=client.post('/api/dockets',json={'sample':'clean'}).json()['key']
    return key,client.get('/api/dockets/'+key).json()


def test_attachment_includes_evidence_and_manifest(exported):
    doc=exported.json()
    assert exported.headers['Content-Disposition'].startswith('attachment; filename="appeal-')
    assert (doc['document'],doc['minutes_balance'])==('泊位理赔证据',0)
    assert digest(doc['evidence'])==doc['manifest']['evidence_sha256']


def test_export_hides_input_plates(exported):
    assert all(plate not in exported.text for plate in ('粤B01234','粤ｂ ０１２３４','粤B11111','粤B22222','"plate"'))
    assert len(exported.json()['evidence']['identity']['stays'][0]['vehicle'])==64


def test_tampered_attachment_cannot_be_issued(exported):
    doc=deepcopy(exported.json()); doc['evidence']['pricing']['due_cents']+=1
    with pytest.raises(DocketError) as err:
        issue(doc)
    assert err.value.code=='DIGEST_BAD'


def test_berth_invariant_before_signing(exported):
    doc=deepcopy(exported.json()); doc['evidence']['settlement']['berths'][0]['vacant']+=1
    doc['manifest']['evidence_sha256']=digest(doc['evidence'])
    with pytest.raises(DocketError) as err:
        issue(doc)
    assert err.value.code=='BERTH_UNBALANCED'


def test_money_invariant_before_signing(exported):
    doc=deepcopy(exported.json()); doc['evidence']['settlement']['lines'][0]['difference_cents']+=1
    doc['manifest']['evidence_sha256']=digest(doc['evidence'])
    with pytest.raises(DocketError) as err:
        issue(doc)
    assert err.value.code=='MONEY_UNBALANCED'


def test_missing_batch_is_not_downloaded(client):
    r=client.get('/api/export/unknown')
    assert (r.status_code,r.json()['code'],r.headers.get('Content-Disposition'))==(404,'DOCKET_MISSING',None)


def test_vehicle_attachment_is_self_verifying(sealed):
    key,record=sealed
    vehicle=claims(record)[0]['vehicle']
    document=issue_vehicle(record,vehicle)
    assert document['document']=='本车泊位理赔证据'
    assert (document['key'],document['vehicle'],document['minutes_balance'])==(key,vehicle,0)
    assert digest(document['evidence'])==document['manifest']['evidence_sha256']
    assert digest({'manifest':document['manifest'],'evidence':document['evidence']})==document['sha256']


def test_vehicle_attachment_matches_batch_anchors(sealed):
    key,record=sealed
    document=issue_vehicle(record,claims(record)[0]['vehicle'])
    batch=document['evidence']['reconciliation']['batch']
    assert batch=={'key':key,'rule':record['manifest']['rule'],'source':record['manifest']['source'],
                   'created_utc':record['manifest']['created_utc'],
                   'evidence_sha256':record['manifest']['evidence_sha256'],
                   'sha256':digest({'manifest':record['manifest'],'evidence':record['evidence']})}
    assert document['manifest']['batch_evidence_sha256']==record['manifest']['evidence_sha256']


def test_vehicle_attachment_excludes_other_vehicles(sealed):
    key,record=sealed
    vehicles=sorted({s['vehicle'] for s in record['evidence']['occupancy']['stays']})
    document=issue_vehicle(record,vehicles[0])
    view=document['evidence']
    assert {s['vehicle'] for s in view['occupancy']['stays']}=={vehicles[0]}
    assert {row['vehicle'] for row in view['settlement']['lines']}=={vehicles[0]}
    assert {row['vehicle'] for row in view['pricing']['stays']}=={vehicles[0]}
    assert all(row.get('vehicle')==vehicles[0] for row in view['settlement']['orphans'])
    assert vehicles[1] not in str(view)
    assert '粤B' not in str(document)


def test_vehicle_dispute_minutes_have_per_minute_evidence(sealed):
    key,record=sealed
    target=next(row['vehicle'] for row in record['evidence']['pricing']['stays']
                if any(segment['code']=='PLATE_OVERLAP' and row['berth']=='B-01'
                       for segment in row['segments']))
    view=issue_vehicle(record,target)['evidence']
    own=view['occupancy']['disputes']
    assert 'B-01' in {row['berth'] for row in own}
    assert len(own)==len({(row['berth'],row['at']) for row in own})
    assert all(row['stay'] in {stop['stay'] for stop in view['occupancy']['stays']} for row in own)
    watch=[row for row in view['occupancy']['counterparts'] if row['code']=='PLATE_OVERLAP']
    assert watch and all(row['same_vehicle'] is False for row in watch)
    assert sum(row['minutes'] for row in watch)==sum(1 for row in own if row['code']=='PLATE_OVERLAP')
    charged=sum(segment['minutes'] for row in view['pricing']['stays']
                for segment in row['segments'] if segment['code']=='PLATE_OVERLAP')
    assert charged==sum(1 for row in own if row['code']=='PLATE_OVERLAP')


def test_same_vehicle_cross_berth_dispute_is_flagged(sealed):
    key,record=sealed
    vehicles=sorted({s['vehicle'] for s in record['evidence']['occupancy']['stays']})
    for vehicle in vehicles:
        view=issue_vehicle(record,vehicle)['evidence']
        for row in view['occupancy']['counterparts']:
            if row['code']=='BERTH_OVERLAP':
                assert row['same_vehicle'] is True and row['stay']


def test_vehicle_attachment_is_byte_identical_on_retry(sealed):
    key,record=sealed
    vehicle=claims(record)[0]['vehicle']
    first=issue_vehicle(record,vehicle)
    second=issue_vehicle(deepcopy(record),vehicle)
    assert first==second
    assert first['appeal']==second['appeal']


def test_vehicle_claim_does_not_add_evidence(client):
    key=client.post('/api/dockets',json={'sample':'dirty'}).json()['key']
    before=client.get('/api/dockets').json()
    vehicle=client.get('/api/claims/'+key).json()['claims'][0]['vehicle']
    first=client.get(f'/api/export/{key}/vehicle/{vehicle}')
    second=client.get(f'/api/export/{key}/vehicle/{vehicle}')
    assert (first.status_code,first.content)==(second.status_code,second.content)
    assert client.get('/api/dockets').json()==before


def test_vehicle_export_accepts_stay_reference(sealed):
    key,record=sealed
    stay=record['evidence']['occupancy']['stays'][0]
    document=issue_vehicle(record,stay['stay'])
    assert document['vehicle']==stay['vehicle']
    assert document['appeal']==issue_vehicle(record,stay['vehicle'])['appeal']


def test_unknown_vehicle_is_rejected(client):
    key=client.post('/api/dockets',json={'sample':'dirty'}).json()['key']
    result=client.get(f'/api/export/{key}/vehicle/unknown')
    assert (result.status_code,result.json()['code'],result.headers.get('Content-Disposition'))==(
        404,'VEHICLE_MISSING',None)


def test_tampered_batch_cannot_issue_vehicle(sealed):
    key,record=sealed
    vehicle=claims(record)[0]['vehicle']
    record=deepcopy(record); record['evidence']['pricing']['due_cents']+=1
    with pytest.raises(DocketError) as err:
        issue_vehicle(record,vehicle)
    assert err.value.code=='DIGEST_BAD'


def test_tampered_segment_blocks_vehicle_issue(sealed):
    key,record=sealed
    vehicle=claims(record)[0]['vehicle']
    record=deepcopy(record)
    segment=next(row for row in record['evidence']['pricing']['stays']
                 if row['vehicle']==vehicle)['segments'][0]
    segment['cents']+=1
    record['manifest']['evidence_sha256']=digest(record['evidence'])
    with pytest.raises(DocketError) as err:
        issue_vehicle(record,vehicle)
    assert err.value.code in {'MONEY_UNBALANCED','STAY_UNBALANCED'}


def test_claim_listing_matches_downloads(sealed):
    key,record=sealed
    listed=claims(record)
    assert len(listed)==len({s['vehicle'] for s in record['evidence']['occupancy']['stays']})
    for row in listed:
        assert row['ok']
        document=issue_vehicle(record,row['vehicle'])
        money=document['evidence']['settlement']['money']
        assert (row['appeal'],row['due_cents'],row['paid_cents'],row['difference_cents'])==(
            document['appeal'],money['due_cents'],money['paid_cents'],money['difference_cents'])
        assert row['receipts']==document['evidence']['reconciliation']['receipts']


def test_claim_listing_surfaces_per_vehicle_failure(monkeypatch,sealed):
    import app.archive.appeal as appeal
    key,record=sealed
    vehicle=sorted({s['vehicle'] for s in record['evidence']['occupancy']['stays']})[0]
    real=appeal._vehicle_view
    def broken(manifest,base,target,basis,batch_sha):
        if target==vehicle:
            raise DocketError('DISPUTE_UNBALANCED','本车争议分钟与本车凭据不符',409)
        return real(manifest,base,target,basis,batch_sha)
    monkeypatch.setattr(appeal,'_vehicle_view',broken)
    listed={row['vehicle']:row for row in claims(record)}
    assert listed[vehicle]['ok'] is False and listed[vehicle]['code']=='DISPUTE_UNBALANCED'
    assert all(row['ok'] for other,row in listed.items() if other!=vehicle)


def test_vehicle_totals_roll_up_to_batch(sealed):
    key,record=sealed
    listed=claims(record)
    money=record['evidence']['settlement']['money']
    assert sum(row['due_cents'] for row in listed)==money['due_cents']
    assert sum(row['paid_cents'] for row in listed)==money['paid_cents']
    assert sum(row['difference_cents'] for row in listed)==money['difference_cents']
    totals=record['evidence']['settlement']['totals']
    shared=sum(1 for row in record['evidence']['occupancy']['disputes'] if row['code']=='PLATE_OVERLAP')
    assert sum(row['occupied'] for row in listed)-shared==totals['scheduled']-totals['vacant']
    assert sum(row['disputed'] for row in listed)==totals['disputed']+shared


def test_counterpart_digest_links_two_vehicle_certificates(sealed):
    key,record=sealed
    views={row['vehicle']:issue_vehicle(record,row['vehicle'])['evidence'] for row in claims(record)}
    linked=0
    for vehicle,view in views.items():
        own={digest(stop) for stop in view['occupancy']['stays']}
        for row in view['occupancy']['counterparts']:
            if row['same_vehicle']:
                assert row['record_sha256'] in own
                continue
            assert row['record_sha256'] not in own
            if any(row['record_sha256'] in {digest(stop) for stop in peer['occupancy']['stays']}
                   for other,peer in views.items() if other!=vehicle):
                linked+=1
    assert linked>=1


def test_counterpart_of_other_vehicle_carries_no_identity(sealed):
    key,record=sealed
    for vehicle in sorted({row['vehicle'] for row in record['evidence']['occupancy']['stays']}):
        view=issue_vehicle(record,vehicle)['evidence']
        own_stays={row['stay'] for row in view['occupancy']['stays']}
        for row in view['occupancy']['counterparts']:
            assert 'vehicle' not in row
            if row['same_vehicle']:
                assert row['stay'] in own_stays and row['record_sha256']==digest(
                    next(stop for stop in view['occupancy']['stays'] if stop['stay']==row['stay']))
            else:
                assert 'stay' not in row


def test_vehicle_claim_page_signs_one_vehicle(client):
    key=client.post('/api/dockets',json={'sample':'dirty'}).json()['key']
    claims_rows=client.get('/api/claims/'+key).json()['claims']
    vehicle=claims_rows[0]['vehicle']
    others=[row['vehicle'] for row in claims_rows if row['vehicle']!=vehicle]
    page=client.get('/clearing/appeal',params={'key':key,'vehicle':vehicle})
    assert page.status_code==200
    assert f'/api/export/{key}/vehicle/{vehicle}"' in page.text
    panel=page.text.split('id="signed-vehicle"',1)[1]
    assert '本车结论' in panel and '争议分钟凭据' in panel and '重叠对账' in panel
    assert all(token not in panel for token in others)


def test_claim_page_without_vehicle_lists_only(client):
    key=client.post('/api/dockets',json={'sample':'dirty'}).json()['key']
    page=client.get('/clearing/appeal',params={'key':key})
    assert page.status_code==200 and '本车证据签发' in page.text
    assert 'id="signed-vehicle"' not in page.text


def test_vehicle_export_route_sets_attachment_name(client):
    key=client.post('/api/dockets',json={'sample':'dirty'}).json()['key']
    vehicle=client.get('/api/claims/'+key).json()['claims'][0]['vehicle']
    first=client.get(f'/api/export/{key}/vehicle/{vehicle}')
    second=client.get(f'/api/export/{key}/vehicle/{vehicle}')
    assert first.status_code==200 and first.content==second.content
    appeal=first.json()['appeal']
    assert first.headers['Content-Disposition']==f'attachment; filename="appeal-{key}-{appeal}.json"'
    assert first.json()['appeal']==first.json()['manifest']['appeal']


def test_every_dispute_minute_is_witnessed(sealed):
    key,record=sealed
    for vehicle in sorted({row['vehicle'] for row in record['evidence']['occupancy']['stays']}):
        view=issue_vehicle(record,vehicle)['evidence']
        own={(row['berth'],row['at'],row['code']) for row in view['occupancy']['disputes']}
        seen=set()
        from app.curb.timeaxis import minute,span
        for row in view['occupancy']['counterparts']:
            for when in span(minute(row['start']),minute(row['end'])):
                seen.add((row['berth'],when.isoformat(),row['code']))
        assert own<=seen


def test_clean_sample_vehicle_has_no_dispute_evidence(sealed_clean):
    key,record=sealed_clean
    for vehicle in sorted({row['vehicle'] for row in record['evidence']['occupancy']['stays']}):
        view=issue_vehicle(record,vehicle)['evidence']
        assert view['occupancy']['disputes']==[] and view['occupancy']['counterparts']==[]
        assert view['reconciliation']['disputed_minutes']==0


def test_per_stay_minutes_roll_up_to_vehicle_berths(sealed):
    key,record=sealed
    for vehicle in sorted({row['vehicle'] for row in record['evidence']['occupancy']['stays']}):
        view=issue_vehicle(record,vehicle)['evidence']
        clocks=view['occupancy']['minutes']
        assert [clock['stay'] for clock in clocks]==[row['stay'] for row in view['occupancy']['stays']]
        for clock in clocks:
            assert clock['occupied']==clock['disputed']+clock['billed']
        for berth in view['settlement']['berths']:
            rows=[clock for clock in clocks if clock['berth']==berth['berth']]
            assert (sum(row['occupied'] for row in rows),
                    sum(row['disputed'] for row in rows))==(berth['occupied'],berth['disputed'])
        assert sum(clock['disputed'] for clock in clocks)==view['reconciliation']['disputed_minutes']
