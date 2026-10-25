"""Exercise the actual handler with fake events; no QQ or map network calls."""
import ast,asyncio,time,types
from pathlib import Path

def test_group_lookup_scope_and_private_admin():
    tree=ast.parse((Path(__file__).parents[1]/'main.py').read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='Arcades')
    method=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='handle')
    method.decorator_list=[]
    for arg in method.args.args:arg.annotation=None
    ns={'time':time,'GAMES':{'舞萌':'maimai','中二':'chuni'},'DATA_NOTICE':'来源说明','from_components':lambda x:x}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'handler','exec'),ns)
    class Event:
        def __init__(self,text='',group='A',user='10001',location=None):self.message_str=text;self.group=group;self.user=user;self.location=location;self.answers=[];self.stopped=False
        def get_platform_id(self):return 'fixture'
        def get_sender_id(self):return self.user
        def get_self_id(self):return '20000'
        def get_group_id(self):return self.group
        def is_private_chat(self):return self.group is None
        def is_admin(self):return True
        def get_messages(self):return self.location
        def stop_event(self):self.stopped=True
        def plain_result(self,text):return text
        async def send(self,text):self.answers.append(text)
    async def run():
        obj=types.SimpleNamespace(config={'platform_id':'fixture'},sessions={},busy=set())
        calls=[]
        async def process(state,text,location):calls.append((state['game'],location));return '附近机厅',True
        obj.process=process
        async def handle(e):await ns['handle'](obj,e);return e
        e=await handle(Event('查舞萌'));assert '当前聊天' in e.answers[0]
        await handle(Event(location='B location',group='B'))
        await handle(Event(location='other user',user='10002'))
        assert not calls
        await handle(Event('查中二',group=None))
        await handle(Event(location='A location'));assert calls==[('maimai','A location')]
        await handle(Event(location='private location',group=None));assert calls[-1]==('chuni','private location')
        e=await handle(Event('增加舞萌'));assert '管理操作' in e.answers[0] and not obj.sessions
        await handle(Event('查舞萌'));await handle(Event('取消音游操作'));assert not obj.sessions
        await handle(Event(location='unsolicited'));assert len(calls)==2
    asyncio.run(run())
