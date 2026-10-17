import unittest
from unittest.mock import patch
from copy import deepcopy
from muli_sorter.snapshot_runtime import DockerRuntime
from muli_sorter.snapshot_references import RetentionBlocked

class RuntimeTests(unittest.TestCase):
 def setUp(self):
  self.r= DockerRuntime({'classifier_container':'c','archive_container':'a','protected_containers':['1','2','3','4'],'archive_image_id':'sha256:'+'a'*64})
  self.rows={n:{'id':n+'-original','image_id':'sha256:'+'a'*64,'restarts':0,'memory':1,'command':[],'mounts':[],'running':True,'paused':False,'oom':False,'health':'healthy'} for n in self.r.names}
  for row in self.rows.values():row.update(cpu=1,pids=1,user='1000',entrypoint=[],readonly=True,cap_drop=[],security=[],restart_policy={},ports={},networks=[])
  self.baseline={'containers':deepcopy(self.rows),'classifier':'c','archive':'a'}
 def test_replacement_container_is_never_unpaused(self):
  row=dict(self.rows['c'],id='replacement',paused=True)
  with patch.object(self.r,'snap',return_value=row),patch.object(self.r,'command') as command:
   with self.assertRaises(RetentionBlocked):self.r.resume(self.baseline)
  command.assert_not_called()
 def test_resume_unfreezes_only_original_paused_classifier(self):
  with patch.object(self.r,'snap',side_effect=[dict(self.rows['c'],paused=True),self.rows['c']]),patch.object(self.r,'command') as command:self.r.resume(self.baseline)
  command.assert_called_once_with(['unpause','c'])
 def test_already_resumed_container_never_toggled(self):
  with patch.object(self.r,'snap',return_value=self.rows['c']),patch.object(self.r,'command') as command:self.r.resume(self.baseline)
  command.assert_not_called()
 def test_other_service_drift_or_oom_rejects_verification(self):
  for key,value in [('id','other'),('image_id','other'),('restarts',1),('memory',2),('oom',True),('paused',True),('health','unhealthy')]:
   rows=deepcopy(self.rows);rows['a'][key]=value
   with patch.object(self.r,'snapshots',return_value=rows),self.assertRaises(RetentionBlocked):self.r.verify(self.baseline)
 def test_snapshot_inventory_uses_one_sanitized_call_without_environment(self):
  import json
  data=[dict(self.rows[n],health={'Status':'healthy'}) for n in self.r.names]
  with patch.object(self.r,'command',return_value='\n'.join(json.dumps(x) for x in data)) as command:result=self.r.snapshots(self.r.names)
  args=command.call_args.args[0];self.assertNotIn('Env',' '.join(args));self.assertEqual(args[-6:],self.r.names);self.assertEqual(len(result),6)
 def test_invalid_container_arguments_or_image_digest_are_rejected(self):
  for changes in ({'classifier_container':'--all'},{'archive_image_id':'unbound-tag'}):
   with self.assertRaises(ValueError):DockerRuntime(dict(self.r.config,**changes))
if __name__=='__main__':unittest.main()
