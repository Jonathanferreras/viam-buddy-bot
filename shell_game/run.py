"""One round per invocation. Preview-only by default; --execute actuates hardware."""
import argparse,asyncio,json,os,subprocess,sys,uuid
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parent;ROBOT=ROOT/'robot'
async def prepare(execute):
 from dotenv import load_dotenv
 from viam.robot.client import RobotClient
 from viam.components.arm import Arm
 from viam.components.gripper import Gripper
 from viam.services.motion import MotionClient
 from viam.proto.common import Pose,PoseInFrame,WorldState,GeometriesInFrame,Geometry,RectangularPrism,Vector3
 from viam.proto.service.motion import Constraints,LinearConstraint
 load_dotenv(ROBOT/'.env');robot=await asyncio.wait_for(RobotClient.at_address(os.environ['VIAM_MACHINE_ADDRESS'],RobotClient.Options.with_api_key(api_key=os.environ['VIAM_API_KEY'],api_key_id=os.environ['VIAM_API_KEY_ID'])),30)
 async with robot:
  arm=Arm.from_robot(robot,'arm');grip=Gripper.from_robot(robot,'gripper')
  if await arm.is_moving() or (await grip.is_holding_something(timeout=3)).is_holding_something:raise RuntimeError('Stationary empty arm required; do not restart a held-cup round')
  current=(await robot.transform_pose(PoseInFrame(reference_frame='gripper',pose=Pose(o_z=1)),'world')).pose
  import math
  if not execute:
   if math.dist([current.x,current.y,current.z],[200,-560,350])>1:raise RuntimeError('Preview requires overview pose (200,-560,350). --execute includes planned empty-arm repositioning.')
   return
  try:
   await arm.do_command({'grab_with_torque':{'position':850.,'speed':300.,'torque':10.,'stall_seconds':.2}},timeout=10)
   for _ in range(60):
    if (await grip.do_command({'get':True},timeout=3)).get('pos',0)>=840:break
    await asyncio.sleep(.2)
   else:raise RuntimeError('Full opening not confirmed')
   await arm.do_command({'set_speed':30.,'set_acceleration':30.},timeout=5)
   geom=Geometry(center=Pose(x=200,y=-550,z=0,o_z=1),box=RectangularPrism(dims_mm=Vector3(x=650,y=600,z=300)),label='cups-clearance-envelope')
   orient={k:getattr(current,k) for k in ('o_x','o_y','o_z','theta')}
   for x,y,z in ((current.x,current.y,350),(200,-560,350)):
    ok=await MotionClient.from_robot(robot,'builtin').move(component_name='gripper',destination=PoseInFrame(reference_frame='world',pose=Pose(x=x,y=y,z=z,**orient)),world_state=WorldState(obstacles=[GeometriesInFrame(reference_frame='world',geometries=[geom])]),constraints=Constraints(linear_constraint=[LinearConstraint(line_tolerance_mm=.2,orientation_tolerance_degs=.5)]),timeout=50)
    if not ok:raise RuntimeError('Overview move failed')
  except BaseException:
   await arm.stop(timeout=3);raise

def main():
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--execute',action='store_true');parser.add_argument('--ball-slot',choices=('left','middle','right'),default='middle');parser.add_argument('--swaps',type=int,choices=range(2,7),default=3);args=parser.parse_args()
 import fcntl
 lock=open(ROBOT/'.run.lock','w')
 try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 except BlockingIOError:raise SystemExit('Another round is running')
 asyncio.run(prepare(args.execute))
 name='runs/'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:6];folder=ROBOT/name;folder.mkdir(parents=True);(folder/'overview.json').write_text(json.dumps({'overview_completed':True}))
 env=dict(os.environ,SHUFFLE_RUN=name,SHUFFLE_EXECUTE='1' if args.execute else '0',SHUFFLE_BALL_SLOT=args.ball_slot,SHUFFLE_SWAPS=str(args.swaps))
 try:
  for script in ('capture_stereo.py','detect_cups_auto.py','round.py'):subprocess.run([sys.executable,str(ROBOT/script)],env=env,check=True,stdout=(folder/(script+'.log')).open('w'),stderr=subprocess.STDOUT)
 except KeyboardInterrupt:
  asyncio.run(stop());raise SystemExit('Interrupted; arm stop requested. Inspect state before restarting.')
 report=json.loads((folder/'execution.json').read_text());print('Run log:',folder)
 if report.get('error') or not report.get('full_sequence_preview_passed'):raise SystemExit('Stopped: '+str(report.get('error','Preview did not complete')))
 print('Round completed. Guess left, middle, or right.' if report.get('completed') else 'Preview passed; no shuffle motion issued.')
async def stop():
 from dotenv import load_dotenv
 from viam.robot.client import RobotClient
 from viam.components.arm import Arm
 load_dotenv(ROBOT/'.env');r=await RobotClient.at_address(os.environ['VIAM_MACHINE_ADDRESS'],RobotClient.Options.with_api_key(api_key=os.environ['VIAM_API_KEY'],api_key_id=os.environ['VIAM_API_KEY_ID']))
 async with r:await Arm.from_robot(r,'arm').stop(timeout=3)
if __name__=='__main__':main()
