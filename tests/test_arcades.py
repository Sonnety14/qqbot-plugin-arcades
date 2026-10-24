import json
import pytest
from astrbot_plugin_arcades.locations import coordinates, distance, parse_text, from_components
from astrbot_plugin_arcades.store import Store
from astrbot_plugin_arcades.nearcade_client import NearcadeClient, decode_body, parse_coordinates, UpstreamBlockedError
from astrbot_plugin_arcades.snapshot_store import SnapshotStore, SOURCE_NEARCADE
from astrbot_plugin_arcades.nearcade_sync import sync_nearcade

def shop(n=1, title=1):
    return {'id':n,'name':f'测试机厅{n}','address':{'general':['示例城市'],'detailed':'示例路'},
            'location':{'type':'Point','coordinates':[104.0,30.0]},
            'games':[{'titleId':title,'quantity':1}],'isClosed':False}

def payload(shops):
    return {'shops':shops,'totalCount':len(shops),'currentPage':1,'totalPages':1,'hasNextPage':False,'hasPrevPage':False}

def client(data):
    return NearcadeClient(fetcher=lambda *args:json.dumps(data).encode(),sleep=lambda _:None,min_interval=0)

def test_native_location_and_axis_order():
    data=from_components([{'type':'location','data':{'lat':30,'lon':104,'title':'示例'}}])
    assert (data['lat'],data['lng'])==(30,104)
    assert parse_coordinates({'coordinates':[104,30]})==(30,104)
    assert distance(30,104,30,104)==0

@pytest.mark.parametrize('lat,lng',[(True,104),(float('nan'),104),(91,104),(30,181),(0,0)])
def test_invalid_locations(lat,lng):
    with pytest.raises(ValueError):coordinates(lat,lng)

def test_manual_input():
    assert parse_text('示例 | 示例地址 | 30,104')['lat']==30
    assert parse_text('示例 | 示例地址')['lat'] is None

def test_unrelated_card_is_not_location():
    assert from_components([{'type':'json','data':{'data':{'app':'other','lat':30,'lng':104}}}]) is None

def test_sync_manual_and_tombstone_survive(tmp_path):
    store=Store(tmp_path/'query.sqlite3');snap=SnapshotStore(tmp_path/'snap.sqlite3')
    snap.set_source_state(SOURCE_NEARCADE,'enabled','true')
    ident=store.add('maimai',{'name':'人工示例','address':'示例地址','lat':30,'lng':104})
    data=payload([shop(1,1),shop(2,3)])
    outcome=sync_nearcade(store,snap,client=client(data))
    assert outcome.ok
    assert len(store.nearest('maimai',{'lat':30,'lng':104}))==2
    assert len(store.nearest('chuni',{'lat':30,'lng':104}))==1
    assert store.remove('maimai','nearcade-1')
    assert sync_nearcade(store,snap,client=client(data)).ok
    assert [p['id'] for p in store.nearest('maimai',{'lat':30,'lng':104})]==[ident]

def test_failed_sync_preserves_query_data(tmp_path):
    store=Store(tmp_path/'query.sqlite3');snap=SnapshotStore(tmp_path/'snap.sqlite3')
    snap.set_source_state(SOURCE_NEARCADE,'enabled','true')
    assert sync_nearcade(store,snap,client=client(payload([shop(1,1),shop(2,3)]))).ok
    before=store.all('maimai')
    outcome=sync_nearcade(store,snap,client=client(payload([])))
    assert not outcome.ok and store.all('maimai')==before

def test_challenge_page_is_not_parsed():
    with pytest.raises(UpstreamBlockedError):decode_body(b'<html>captcha</html>')

def test_unconfigured_source_does_not_fetch(tmp_path):
    store=Store(tmp_path/'query.sqlite3');snap=SnapshotStore(tmp_path/'snap.sqlite3')
    class NoNetwork:
        def __getattr__(self,name):raise AssertionError('unexpected network')
    assert sync_nearcade(store,snap,client=NoNetwork()).status=='skipped'
