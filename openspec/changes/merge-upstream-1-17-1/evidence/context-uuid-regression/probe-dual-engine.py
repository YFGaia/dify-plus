"""Dedicated empty migration databases only; outer transaction always rolls back."""
import json
import logging
from datetime import datetime,timedelta
from types import SimpleNamespace
from uuid import uuid4
from sqlalchemy import select
from sqlalchemy.orm import Session
from app_factory import create_migrations_app
from extensions.ext_database import db
from models.account import Account
from models.model import App,Conversation,AppMode
from models.enums import ConversationFromSource
from models.model_extend import MessageContextExtend
from services import recommended_app_service_extend as module
logging.disable(logging.CRITICAL)
app=create_migrations_app()
with app.app_context():
 engine=db.session.get_bind();dialect=engine.dialect.name
 assert dialect in {'postgresql','mysql'}
 assert db.session.query(Account).count()==0 and db.session.query(App).count()==0
 connection=engine.connect();outer=connection.begin();session=Session(bind=connection,join_transaction_mode='create_savepoint')
 original_db=module.db;module.db=SimpleNamespace(session=session)
 result={'engine':dialect,'fixture_only':True,'outer_rollback':False}
 try:
  ta,tb=str(uuid4()),str(uuid4());apps=[];conversations=[]
  for tenant in [ta,tb]:
   a=App(tenant_id=tenant,mode=AppMode.CHAT,name='Dedicated UUID context probe',enable_site=True,enable_api=True);session.add(a);session.flush();apps.append(a)
  for a,deleted in [(apps[0],False),(apps[1],False),(apps[0],True)]:
   c=Conversation(app_id=a.id,mode=AppMode.CHAT,name='Dedicated context fixture',_inputs={},from_source=ConversationFromSource.CONSOLE,is_deleted=deleted);session.add(c);session.flush();conversations.append(c)
   for i in [0,1]:session.add(MessageContextExtend(conversation_id=c.id,message_id=str(uuid4()),created_at=datetime(2026,1,1)+timedelta(seconds=i)))
  session.flush();scope={'tenant_id':ta,'app_id':apps[0].id,'conversation_id':conversations[0].id}
  legacy=select(Conversation.id).join(App,App.id==Conversation.app_id).where(App.tenant_id==ta,App.id==apps[0].id,Conversation.id==conversations[0].id,Conversation.is_deleted.is_(False))
  try:
   with session.begin_nested():session.scalars(select(MessageContextExtend).where(MessageContextExtend.conversation_id.in_(legacy))).all()
   result['legacy_before']='passed'
  except Exception as error:
   result['legacy_before']='failed';result['legacy_error_type']=type(error).__name__;result['legacy_text_uuid_operator_error']='character varying = uuid' in str(error)
  if dialect=='postgresql':assert result['legacy_before']=='failed' and result['legacy_text_uuid_operator_error']
  else:assert result['legacy_before']=='passed'
  positive=module.RecommendedAppService.message_context(**scope);assert len(positive)==2
  result['cast_read_positive']='passed'
  for foreign in [{'tenant_id':tb,'app_id':apps[0].id,'conversation_id':conversations[0].id},{'tenant_id':ta,'app_id':apps[1].id,'conversation_id':conversations[0].id},{'tenant_id':ta,'app_id':apps[0].id,'conversation_id':conversations[2].id}]:
   assert module.RecommendedAppService.message_context(**foreign)==[]
   assert module.RecommendedAppService.delete_message_context(**foreign,message_id=positive[0])=='ok'
   assert session.query(MessageContextExtend).count()==6
  result['foreign_app_tenant_deleted_read_delete_guard']='passed'
  assert module.RecommendedAppService.delete_message_context(**scope,message_id=positive[0])=='ok'
  assert module.RecommendedAppService.message_context(**scope)==[positive[1]]
  assert session.query(MessageContextExtend).count()==5
  result['positive_delete_selected_marker_only']='passed'
 finally:
  session.close();module.db=original_db;outer.rollback();connection.close()
  db.session.expire_all();result['outer_rollback']=db.session.query(App).count()==0 and db.session.query(Conversation).count()==0 and db.session.query(MessageContextExtend).count()==0
 assert result['outer_rollback']
 print('SAFE_RESULT='+json.dumps(result))
