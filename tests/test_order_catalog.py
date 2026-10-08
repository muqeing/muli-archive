import copy
import unittest
from muli_sorter.order_catalog import ORDER_FIELDS,OrderError,read_orders,read_product_codes
from muli_sorter.order_projects import enrich_confirmation,plan_folders
from muli_sorter.review import build_review_model,compile_plan
from test_review import sample_report,draft


def page():
    return {'fields':ORDER_FIELDS,'record_id_list':['recSYNTHETIC1'],'rev':1,'has_more':False,
            'data':[['00999','2026-07-09T10:00:00+08:00','合成客户',['已拍摄'],[{'id':'recPRODUCT1'}],None,None,None]]}


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.resource={'base_token':'SYNTHETIC_RESOURCE_ONLY','table_name':'合成表','product_table_id':'tblSynthetic'}

    def test_projection_and_date_boundaries(self):
        calls=[]
        def reader(args):
            calls.append(args)
            return page()
        d=read_orders(self.resource,['2026-07-09'],reader=reader)
        self.assertEqual(d['orders'][0]['order_id'],'00999')
        self.assertNotIn('订单小计',calls[0])
        self.assertEqual(calls[0][:2],['base','+record-list'])
        p=page();p['data'][0][1]='2026-07-10T10:00:00+08:00'
        with self.assertRaises(OrderError):read_orders(self.resource,['2026-07-09'],reader=lambda _:p)

    def test_pagination_changes_or_duplicates_fail_closed(self):
        for change in ('revision','duplicate'):
            a=page();a['has_more']=True
            b=page()
            if change=='revision':b['rev']=2
            pages=iter([a,b])
            with self.assertRaises(OrderError):read_orders(self.resource,['2026-07-09'],reader=lambda _:next(pages))

    def test_pagination_offsets_and_no_truncation(self):
        a=page();a['has_more']=True
        b=page();b['record_id_list']=['recSYNTHETIC2'];b['data'][0][0]='00998'
        pages=iter([a,b]);offsets=[]
        def reader(args):
            offsets.append(args[-1]);return next(pages)
        self.assertEqual(len(read_orders(self.resource,['2026-07-09'],reader=reader)['orders']),2)
        self.assertEqual(offsets,['0','1'])

    def test_product_read_requires_all_requested_ids(self):
        d={'fields':['产品ID'],'record_id_list':['recPRODUCT1'],'data':[['TEST-01']]}
        self.assertEqual(read_product_codes(self.resource,['recPRODUCT1'],reader=lambda _:d),{'recPRODUCT1':'TEST-01'})
        with self.assertRaises(OrderError):read_product_codes(self.resource,['recPRODUCT1','recPRODUCT2'],reader=lambda _:d)

    def test_existing_order_wins_and_missing_gets_creation_intent_only(self):
        catalog=read_orders(self.resource,['2026-07-09'],reader=lambda _:page())
        def namer(o):return '2026/7月/20260709_'+o['order_id']+'_合成客户_TEST-01'
        result=plan_folders(catalog,[],{'recPRODUCT1':'TEST-01'},namer)
        self.assertEqual(result['intentions'][0]['action'],'create_required')
        self.assertFalse(result['folder_write_authorized'])
        existing={'order_id':'00999','project_id':'dir1','dates':['2026-07-09'],'path':'2026/7月/20260709_00999_已有别名'}
        result=plan_folders(catalog,[existing],{},namer)
        self.assertEqual(result['intentions'][0]['action'],'use_existing')

    def test_ambiguous_or_cancelled_order_never_creates(self):
        catalog=read_orders(self.resource,['2026-07-09'],reader=lambda _:page())
        for mutate in (lambda c:c['orders'].append(copy.deepcopy(c['orders'][0])),lambda c:c['orders'][0].update(stage=['已取消']),lambda c:c['orders'][0].update(product_record_ids=['recPRODUCT1','recPRODUCT2'])):
            c=copy.deepcopy(catalog);mutate(c)
            r=plan_folders(c,[],{'recPRODUCT1':'TEST-01'},lambda _: '2026/7月/20260709_00999_合成')
            self.assertTrue(all(x['action']=='needs_review' for x in r['intentions']))

    def test_missing_folder_is_candidate_only_and_old_draft_is_not_reused(self):
        catalog=read_orders(self.resource,['2026-07-09'],reader=lambda _:page())
        model=build_review_model(sample_report())
        old_draft=draft(model)
        intents=plan_folders(catalog,model['projects'],{'recPRODUCT1':'TEST-01'},lambda _: '2026/7月/20260709_00999_合成_TEST-01')
        enriched=enrich_confirmation(model,catalog,intents)
        added=next(p for p in enriched['projects'] if p.get('exists') is False)
        self.assertTrue(added['name'].startswith('【待建目录】'))
        self.assertNotEqual(enriched['report_id'],model['report_id'])
        self.assertTrue(all(s['project_id'] is None and s['decision']=='pending' for s in enriched['initial_segments']))
        for unit in enriched['units']:
            self.assertEqual(added['project_id'] in unit['candidate_project_ids'],unit['capture_date']=='2026-07-09')
        with self.assertRaises(ValueError):compile_plan(enriched,old_draft)
        plan=draft(enriched)
        plan['segments'][0].update(project_id=added['project_id'],decision='confirmed')
        compiled=compile_plan(enriched,plan)
        self.assertFalse(compiled['executable'])
        self.assertFalse(compiled['media_write_authorized'])
        self.assertEqual(compiled['assignments'][0]['project']['folder_action'],'create_after_confirmed_assignment')


if __name__=='__main__':unittest.main()
