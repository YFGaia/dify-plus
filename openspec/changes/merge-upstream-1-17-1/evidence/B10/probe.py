import logging,json,os,contextlib,io
logging.disable(logging.CRITICAL)
from decimal import Decimal
from uuid import uuid4
assert os.environ['DB_DATABASE']=='dify_b10_acceptance_20260930'
import app
from extensions.ext_database import db
from sqlalchemy import text
from models.account import Account
from models.model import App
from models.api_token_money_extend import ApiTokenMoneyExtend as K,ApiTokenMoneyDailyStatExtend as DS,ApiTokenMoneyMonthlyStatExtend as MS
from models.account_money_extend import AccountMoneyExtend as A
from models.account_money_monthly_stat_extend import AccountMoneyMonthlyStatExtend as AS
from schedule.update_api_token_daily_used_quota_task_extend import update_api_token_daily_used_quota_task_extend as daily
from schedule.update_api_token_monthly_used_quota_task_extend import update_api_token_monthly_used_quota_task_extend as monthly
from schedule.update_account_used_quota_extend import update_account_used_quota_extend as account
result={'scene':'B10','image_id':'sha256:51347867bb65e505d37ba1730db71192ba30904035e2eece6d1c43bb2e108619','mode':'actual canonical image separate synthetic database direct task.run; no delay/worker/beatclock/mail','database':'dify_b10_acceptance_20260930'}
with app.app.app_context():
 assert db.engine.url.database==result['database']
 assert db.session.execute(text('select current_database()')).scalar()==result['database']
 assert db.session.query(Account).count()==db.session.query(App).count()==0
 assert all(db.session.query(c).count()==0 for c in [K,A,DS,MS,AS])
 result['guards']={'zero_real_accounts':True,'zero_apps':True,'zero_initial_task_tables':True,'database_identity_checked':True}
 result['heads']={'upstream':db.session.execute(text('select version_num from alembic_version')).scalar(),'extend':db.session.execute(text('select version_num from alembic_version_extend')).scalar()}
 keys=[str(uuid4()) for _ in range(4)];accounts=[str(uuid4()) for _ in range(2)];result['frozen_key_ids']=keys;result['frozen_account_money_ids']=accounts
 for i,k in enumerate(keys):db.session.add(K(app_token_id=k,accumulated_quota=Decimal('9.1234567')+i,day_used_quota=Decimal('1.1234567')+i,month_used_quota=Decimal('5.7654321')+i,day_limit_quota=Decimal('-1') if i==0 else Decimal('20'),month_limit_quota=Decimal('30'),description='B10 dedicated fixture'))
 for i,a in enumerate(accounts):db.session.add(A(account_id=a,total_quota=Decimal('40.1234567')+i,used_quota=Decimal('3.7654321')+i))
 db.session.commit()
 def keyrows():return {k.app_token_id:{f:str(getattr(k,f)) for f in ['accumulated_quota','day_used_quota','month_used_quota','day_limit_quota','month_limit_quota']} for k in db.session.query(K).all()}
 def acctrows():return {a.account_id:{f:str(getattr(a,f)) for f in ['total_quota','used_quota']} for a in db.session.query(A).all()}
 before=keyrows();abefore=acctrows();result['before']={'keys':before,'account_money':abefore}
 for task in [daily,monthly,account]:
  names=[v['task'] for v in app.celery.conf.beat_schedule.values()];assert names.count(task.name)==1
 result['beat']={task.name:{'unique_registration':True,'task_queue':task._get_exec_options().get('queue'),'schedule':str(next(v['schedule'] for v in app.celery.conf.beat_schedule.values() if v['task']==task.name))} for task in [daily,monthly,account]}
 with contextlib.redirect_stdout(io.StringIO()):daily.run()
 db.session.expire_all();afterdaily=keyrows();assert len(db.session.query(DS).all())==4
 for row in db.session.query(DS).all():assert str(row.day_used_quota)==before[row.app_token_id]['day_used_quota'] and str(row.accumulated_quota)==before[row.app_token_id]['accumulated_quota'] and str(row.day_limit_quota)==before[row.app_token_id]['day_limit_quota']
 for k,v in afterdaily.items():assert Decimal(v['day_used_quota'])==0 and all(v[f]==before[k][f] for f in ['accumulated_quota','month_used_quota','day_limit_quota','month_limit_quota'])
 result['daily']={'snapshot_rows':4,'preusage_snapshot_exact':True,'only_day_reset':True,'readback':afterdaily}
 with contextlib.redirect_stdout(io.StringIO()):monthly.run()
 db.session.expire_all();afterm=keyrows();assert db.session.query(MS).count()==4
 for row in db.session.query(MS).all():assert str(row.month_used_quota)==before[row.app_token_id]['month_used_quota'] and str(row.accumulated_quota)==before[row.app_token_id]['accumulated_quota']
 for k,v in afterm.items():assert Decimal(v['month_used_quota'])==0 and v['accumulated_quota']==before[k]['accumulated_quota'] and v['day_limit_quota']==before[k]['day_limit_quota'] and v['month_limit_quota']==before[k]['month_limit_quota']
 result['monthly']={'snapshot_rows':4,'preusage_snapshot_exact':True,'accumulated_not_reset':True,'limits_not_changed':True,'readback':afterm}
 with contextlib.redirect_stdout(io.StringIO()):account.run()
 db.session.expire_all();aa=acctrows();assert db.session.query(AS).count()==2
 for row in db.session.query(AS).all():assert str(row.used_quota)==abefore[row.account_id]['used_quota'] and str(row.total_quota)==abefore[row.account_id]['total_quota']
 for k,v in aa.items():assert Decimal(v['used_quota'])==0 and v['total_quota']==abefore[k]['total_quota']
 result['account']={'snapshot_rows':2,'preusage_and_total_snapshot_exact':True,'only_used_reset':True,'readback':aa}
 result['status']='passed_bounded_task_effects_and_unique_schedule_registration';result['periodic_clock_triggered']=False;result['real_accounts_and_apps_after']=db.session.query(Account).count()+db.session.query(App).count();assert result['real_accounts_and_apps_after']==0
 print(json.dumps(result,indent=2))
