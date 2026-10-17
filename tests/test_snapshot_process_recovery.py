"""OS termination releases the gate; reconciliation never repeats deletions."""
import multiprocessing,time,unittest
from pathlib import Path
from test_snapshot_retention import Fixture,Runtime
from muli_sorter.snapshot_references import References
from muli_sorter.snapshot_cleanup import Cleanup,Policy

class FileRuntime(Runtime):
 def __init__(self,marker,pipe=None):super().__init__();self.marker=Path(marker);self.pipe=pipe
 def pause(self,b):
  self.marker.write_text('paused')
  if self.pipe:
   self.pipe.send('paused')
   while True:time.sleep(1)
 def resume(self,b):self.marker.write_text('resumed')

def child(state,queue,marker,pipe,expected_package):
 import muli_sorter
 assert str(Path(muli_sorter.__file__).resolve().parent)==expected_package
 refs=References(state,queue);policy=Policy(enabled=True,maximum_generations=2)
 Cleanup(refs,policy,FileRuntime(marker,pipe)).run(execute=True,confirmed_policy=policy.digest())

class ProcessTests(Fixture):
 def test_killed_runner_can_unfreeze_same_owner_without_replaying_deletion(self):
  import muli_sorter
  context=multiprocessing.get_context('spawn');receive,send=context.Pipe(duplex=False);marker=self.root/'freezer-state'
  process=context.Process(target=child,args=(str(self.state),str(self.queue),str(marker),send,str(Path(muli_sorter.__file__).resolve().parent)));process.start();send.close()
  try:
   self.assertTrue(receive.poll(15),'child did not reach freezer boundary');self.assertEqual(receive.recv(),'paused')
   process.kill();process.join(10);self.assertFalse(process.is_alive());self.assertEqual(marker.read_text(),'paused')
   before=sorted(p.name for p in (self.queue/'combined').iterdir())
   policy=Policy(enabled=True,maximum_generations=2);result=Cleanup(self.refs,policy,FileRuntime(marker)).recover_freezer(confirmed_policy=policy.digest())
   self.assertTrue(result['requires_review']);self.assertEqual(marker.read_text(),'resumed');self.assertEqual(before,sorted(p.name for p in (self.queue/'combined').iterdir()))
  finally:
   if process.is_alive():process.kill();process.join(10)
   receive.close()
if __name__=='__main__':unittest.main()
