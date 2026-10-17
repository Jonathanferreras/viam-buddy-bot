"""Single continuous 3-transfer swap. One initial scene; no between-transfer scans.
Collision planning, settled-jaw confirmation and grip-loss stops remain active.
"""
import asyncio,json,os,math,time,itertools,secrets
from google.protobuf.json_format import MessageToJson
from google.protobuf.struct_pb2 import Struct
from viam.proto.service.motion import MoveRequest
from viam.proto.component.arm import JointPositions,MoveOptions
from pathlib import Path
import numpy as np
from dotenv import load_dotenv
from viam.robot.client import RobotClient
from viam.components.arm import Arm
from viam.components.gripper import Gripper
from viam.services.motion import MotionClient
from viam.proto.common import Pose,PoseInFrame,WorldState,GeometriesInFrame,Geometry,RectangularPrism,Vector3
from viam.proto.service.motion import Constraints,LinearConstraint
ROOT=Path(__file__).resolve().parent;FIELDS=('x','y','z','o_x','o_y','o_z','theta')
def distance(p,a,b):
 d=np.array(b)-a;n=d@d;t=np.clip((np.array(p)-a)@d/n,0,1) if n else 0
 return float(np.linalg.norm(np.array(p)-(np.array(a)+t*d)))
async def main():
 tuning=json.loads((ROOT/'fast-shuffle-tuning.json').read_text())
 jaw_speed=float(tuning['jaw_speed']);open_position=float(tuning['open_position'])
 if not 100<=jaw_speed<=500 or not 650<=open_position<=850:raise RuntimeError('Gripper tuning outside trial bounds')
 if open_position<850 and not (tuning.get('opening_clearance_verified') or tuning.get('allow_estimated_opening_trial')):raise RuntimeError('Reduced opening needs clearance validation or an explicit estimated-opening trial')
 release_extra_mm=tuning.get('release_extra_mm')
 units_per_mm=tuning.get('position_units_per_mm')
 if release_extra_mm is not None and (units_per_mm is None or not tuning.get('opening_clearance_verified')):raise RuntimeError('20 mm release needs verified gripper position scale')
 last_hold_position=None
 cached_plans=[];plan_index=0
 setting_path=ROOT/'shuffle-speed-setting.json';setting=json.loads(setting_path.read_text()) if setting_path.exists() else {}
 speed=float(setting.get('speed_degrees_per_second',3)) if setting.get('applied') else 3.
 if not 1<=speed<=180:raise RuntimeError('Requested speed outside configured 1–180 degree/second range')
 acceleration=float(setting.get('acceleration_degrees_per_second_squared',2)) if setting.get('applied') else 2.
 if not 0<acceleration<=300:raise RuntimeError('Acceleration outside configured range')
 folder=ROOT/os.environ['SHUFFLE_RUN'];out=folder/'execution.json'
 if out.exists():raise RuntimeError('Attempt already recorded; do not replay')

 report={'completed':False,'cup_transfers_completed':0,'speed_degrees_per_second':speed,'acceleration_degrees_per_second_squared':acceleration,'between_transfer_scans':False,'collision_checking':True,'grip_loss_stops':True,'events':[]}
 def save():out.write_text(json.dumps(report,indent=2)+'\n')
 def event(name,**data):report['events'].append({'event':name,'time_monotonic_seconds':time.monotonic(),**data});save();print(name,flush=True)
 # Use the independently measured offsets of the two prior released cups.
 b=json.loads((ROOT/'middle-left-swap-02/parking-reacquisition.json').read_text())['mean_center_world_mm'];a=json.loads((ROOT/'middle-left-swap-03/middle-reacquisition.json').read_text())['mean_center_world_mm']
 oldb=json.loads((ROOT/'middle-left-swap-02/execution.json').read_text());olda=json.loads((ROOT/'middle-left-swap-03/execution.json').read_text())
 pb=next(e['actual'] for e in oldb['events'] if e['event']=='move_completed' and e['phase']=='slide');pa=next(e['actual'] for e in olda['events'] if e['event']=='move_completed' and e['phase']=='slide')
 biases=[np.array([p[k]-center[i] for i,k in enumerate(('x','y'))]) for p,center in ((pb,b),(pa,a))]
 if np.linalg.norm(biases[0]-biases[1])>10:raise RuntimeError('Placement offsets disagree')
 bias=np.mean(biases,axis=0)
 if np.linalg.norm(bias)>30:raise RuntimeError('Placement compensation outside bound')
 scanfile=ROOT/'stereo-scan/automatic-analysis.json';scan=json.loads(scanfile.read_text());meta=json.loads((ROOT/'stereo-scan/capture.json').read_text());R=np.array(meta['basis_world_columns']).T;origin=np.array([meta['camera_world'][k] for k in ('x','y','z')])
 cups=[c['cap_fit'] for c in scan['object_clusters'] if c['points']>1500 and c.get('cap_fit') and 20<c['cap_fit']['radius_mm']<40 and c['cap_fit']['boundary_rms_mm']<3]
 if len(cups)!=3:raise RuntimeError('Initial scene requires three cup candidates')
 def pixel_x(c):
  p=R.T@(np.array(c['center_world_mm'])-origin);return p[0]/p[2]
 cups.sort(key=pixel_x);slots={name:np.array(cup['center_world_mm'][:2]) for name,cup in zip(('left','middle','right'),cups)}
 line=slots['right']-slots['left'];line/=np.linalg.norm(line);normal=np.array([-line[1],line[0]])
 if (R.T@np.r_[normal,0])[1]>0:normal=-normal
 slots['parking']=slots['middle']+normal*160
 occupied={'left':'L','middle':'M','right':'R'};plan=[]
 pairs=[secrets.choice(('left','right')) for _ in range(int(os.environ.get('SHUFFLE_SWAPS','3')))]
 while len(set(pairs))<2:
  pairs=[secrets.choice(('left','right')) for _ in range(int(os.environ.get('SHUFFLE_SWAPS','3')))]
 transfers=[transfer for side in pairs for transfer in (('middle','parking'),(side,'middle'),('parking',side))]
 report['private_pair_sequence']=pairs;report['nominal_layout_only']=False;report['robot_motion_issued']=False
 for index,(source,target) in enumerate(transfers):
  moving=occupied.pop(source);source_tcp=slots[source]+bias;target_tcp=slots[target]+bias;path=[source_tcp,target_tcp]
  if index%3==2:
   candidates=[[source_tcp,target_tcp]]
   for offset in (150,170,190,210,230,250,270):
    for fraction in (.15,.25,.35,.45,.55,.65,.75,.85,.95):
     bend=slots['middle']+normal*offset+(slots[target]-slots['middle'])*fraction+bias
     candidates.append([source_tcp,bend,target_tcp])
   stationary=list(slots[s] for s in occupied)
   def valid(route):
    return all(distance(p,x,y)>=130 and distance(p,x-bias,y-bias)>=115 for p in stationary for x,y in zip(route,route[1:]))
   candidates=[route for route in candidates if valid(route)]
   if not candidates:raise RuntimeError('No shorter route meets clearances')
   path=max(candidates,key=lambda route:min(distance(p,x,y) for p in stationary for x,y in zip(route,route[1:])))
  obstacles={s:slots[s] for s in occupied}
  tool_clear=min(distance(p,x,y) for p in obstacles.values() for x,y in zip(path,path[1:]));cup_clear=min(distance(p,x-bias,y-bias) for p in obstacles.values() for x,y in zip(path,path[1:]))
  if tool_clear<130 or cup_clear<115:raise RuntimeError('Route does not meet tool/cup clearance bounds')
  plan.append({'source':source,'target':target,'cup':moving,'path':[p.tolist() for p in path],'tool_clearance_mm':tool_clear,'cup_clearance_mm':cup_clear});occupied[target]=moving
 ball_slot=os.environ.get('SHUFFLE_BALL_SLOT','middle')
 if os.environ.get('SHUFFLE_SKIP_REVEAL')!='1':plan.insert(0,{'source':ball_slot,'target':ball_slot,'cup':{'left':'L','middle':'M','right':'R'}[ball_slot],'path':[(slots[ball_slot]+bias).tolist()],'reveal':True})
 report['slots']={s:p.tolist() for s,p in slots.items()};report['placement_compensation_xy_mm']=bias.tolist();report['plan']=plan;save();occupied={'left':'L','middle':'M','right':'R'}
 load_dotenv(ROOT/'.env');robot=await asyncio.wait_for(RobotClient.at_address(os.environ['VIAM_MACHINE_ADDRESS'],RobotClient.Options.with_api_key(api_key=os.environ['VIAM_API_KEY'],api_key_id=os.environ['VIAM_API_KEY_ID'])),30)
 async with robot:
  arm=Arm.from_robot(robot,'arm');grip=Gripper.from_robot(robot,'gripper');motion=MotionClient.from_robot(robot,'builtin')
  async def held():return (await grip.is_holding_something(timeout=3)).is_holding_something
  async def jaw(opening):
   nonlocal last_hold_position
   release_position=open_position
   if opening and release_extra_mm is not None:
    if last_hold_position is None:raise RuntimeError('No settled hold position for relative release')
    release_position=min(850.,last_hold_position+float(release_extra_mm)*float(units_per_mm))
   if await arm.is_moving():raise RuntimeError('Arm moving before jaw action')
   if opening and last_hold_position is not None and release_position-last_hold_position<85:raise RuntimeError('Estimated release opening leaves less than 10 mm gap increase; use wider opening')
   event('jaw_started',opening=opening)
   await arm.do_command({'grab_with_torque':{'position':release_position if opening else 450.,'speed':jaw_speed,'torque':10.,'stall_seconds':float(tuning.get('stall_seconds',.2))}},timeout=10)
   event('jaw_command_returned',opening=opening)
   samples=[]
   for _ in range(80):
    state=await grip.is_holding_something(timeout=3);pos=(await grip.do_command({'get':True},timeout=3)).get('pos',0);status=int((state.meta or {}).get('status',1))&3
    valid=(not state.is_holding_something and status==0 and pos>=release_position-10) if opening else (state.is_holding_something and status==2 and 450<pos<800)
    samples=(samples+[pos])[-3:] if valid else []
    if len(samples)==3 and max(samples)-min(samples)<=2:
     if not opening:last_hold_position=float(np.mean(samples))
     event('released' if opening else 'stable_hold',jaw_positions=samples);return
    await asyncio.sleep(.15)
   raise RuntimeError('Jaws did not settle; no further motion')
  def world(moving):
   geoms=[Geometry(center=Pose(x=200,y=-550,z=-10,o_z=1),box=RectangularPrism(dims_mm=Vector3(x=650,y=600,z=50)),label='playing-surface-margin')]
   for slot,cup in occupied.items():
    if cup==moving:continue
    x,y=slots[slot];geoms.append(Geometry(center=Pose(x=float(x),y=float(y),z=65,o_z=1),box=RectangularPrism(dims_mm=Vector3(x=110,y=110,z=150)),label='stationary-cup-'+slot))
   return WorldState(obstacles=[GeometriesInFrame(reference_frame='world',geometries=geoms)])
  async def go(xy,z,moving,holding,phase):
   nonlocal plan_index
   if await held()!=holding:raise RuntimeError('Grip state mismatch before '+phase)
   target=Pose(**{**orientation,'x':float(xy[0]),'y':float(xy[1]),'z':z});event('motion',phase=phase,target={k:getattr(target,k) for k in ('x','y','z')})
   if tuning.get('execute_cached_trajectories'):
    trajectory=cached_plans[plan_index];plan_index+=1
    actual=(await arm.get_joint_positions(timeout=3)).values
    first=[math.degrees(v) for v in trajectory[0]['arm']]
    if len(actual)!=6 or max(abs(a-b) for a,b in zip(actual,first))>1:raise RuntimeError('Cached trajectory start no longer matches arm')
    if any(len(row.get('arm',[]))!=6 or not all(math.isfinite(v) for v in row['arm']) for row in trajectory):raise RuntimeError('Invalid cached trajectory')
    positions=[JointPositions(values=[math.degrees(v) for v in row['arm']]) for row in trajectory]
    task=asyncio.create_task(arm.move_through_joint_positions(positions,options=MoveOptions(max_vel_degs_per_sec=speed,max_acc_degs_per_sec2=acceleration),timeout=50))
   else:
    task=asyncio.create_task(motion.move(component_name='gripper',destination=PoseInFrame(reference_frame='world',pose=target),world_state=world(moving),constraints=Constraints(linear_constraint=[LinearConstraint(line_tolerance_mm=.2,orientation_tolerance_degs=.5)]),timeout=50))
   try:
    async with asyncio.timeout(55):
     while not task.done():
      done,_=await asyncio.wait([task],timeout=.25)
      if done:break
      if await held()!=holding:await arm.stop(timeout=3);raise RuntimeError('Grip state changed during '+phase)
     result=await task
     if not tuning.get('execute_cached_trajectories') and not result:raise RuntimeError('Motion planner returned false')
     if tuning.get('execute_cached_trajectories'):
      actual=(await arm.get_joint_positions(timeout=3)).values
      end=[math.degrees(v) for v in trajectory[-1]['arm']]
      if max(abs(a-b) for a,b in zip(actual,end))>1:raise RuntimeError('Cached trajectory endpoint not reached')
     event('motion_completed',phase=phase)
   finally:
    if not task.done():task.cancel();await arm.stop(timeout=3)
  try:
   if await arm.is_moving() or await held() or (await grip.do_command({'get':True},timeout=3)).get('pos',0)<650:raise RuntimeError('Empty stationary preview start required')
   current=(await robot.transform_pose(PoseInFrame(reference_frame='gripper',pose=Pose(o_z=1)),'world')).pose
   report['actual_start_pose']={k:getattr(current,k) for k in FIELDS}
   if math.dist([current.x,current.y,current.z],[200,-560,350])>1:raise RuntimeError('Overview changed')
   report['layout_age_seconds']=time.time()-scanfile.stat().st_mtime
   if report['layout_age_seconds']>300:raise RuntimeError('Fresh layout required')
   orientation={k:getattr(current,k) for k in FIELDS}
   configuration=None
   preview_occupied=dict(occupied)
   for item in plan:
    cup=item['cup'];source=item['source'];target=item['target'];path=item['path']
    goals=([(path[0],130),(path[0],100),(path[0],120),(path[0],100),(path[0],130)] if item.get('reveal') else [(path[0],130),(path[0],100)]+[(xy,100) for xy in path[1:]]+[(path[-1],130)])
    for xy,z in goals:
     destination=Pose(**{**orientation,'x':float(xy[0]),'y':float(xy[1]),'z':z})
     params={'timeout':20}
     if configuration is not None:params['start_state']={'configuration':configuration}
     extra=Struct();extra.update(params)
     request=MoveRequest(name='builtin',component_name='gripper',destination=PoseInFrame(reference_frame='world',pose=destination),world_state=world(cup),constraints=Constraints(linear_constraint=[LinearConstraint(line_tolerance_mm=.2,orientation_tolerance_degs=.5)]),extra=extra)
     response=await motion.do_command({'plan':MessageToJson(request)},timeout=30)
     trajectory=response.get('plan')
     if not trajectory or response.get('plan_partialwp') is not None:raise RuntimeError('Complete sequence preview failed')
     cached_plans.append(trajectory)
     configuration=trajectory[-1]
     event('preview_passed',target={'x':float(xy[0]),'y':float(xy[1]),'z':z})
    occupied[target]=occupied.pop(source)
   report['private_final_occupied']=dict(occupied)
   report['private_ball_final_slot']=next(k for k,v in occupied.items() if v=={'left':'L','middle':'M','right':'R'}[os.environ.get('SHUFFLE_BALL_SLOT','middle')])
   occupied.clear();occupied.update(preview_occupied)
   (folder/'cached-plans.json').write_text(json.dumps(cached_plans))
   report['full_sequence_preview_passed']=True;report['gripper_tuning']=tuning;save()
   if os.environ.get('SHUFFLE_EXECUTE')!='1':event('preview_only_finished');return
   report['robot_motion_issued']=True;save()
   await arm.do_command({'set_speed':speed,'set_acceleration':acceleration},timeout=5);event('started')
   configured_speed=speed;configured_acceleration=acceleration
   for transfer_index,item in enumerate(plan):
    speed=configured_speed
    acceleration=configured_acceleration
    await arm.do_command({'set_speed':speed,'set_acceleration':acceleration},timeout=5)
    cup=item['cup'];source=item['source'];target=item['target'];path=item['path']
    if item.get('reveal'):
     speed=15.;acceleration=15.
     await arm.do_command({'set_speed':speed,'set_acceleration':acceleration},timeout=5)
     await go(path[0],130,cup,False,'reveal approach');await go(path[0],100,cup,False,'reveal descend');await jaw(False)
     await go(path[0],120,cup,True,'reveal lift');event('reveal_pause_started');await asyncio.sleep(2)
     await go(path[0],100,cup,True,'reveal lower');await jaw(True);await go(path[0],130,cup,False,'reveal retreat');event('reveal_completed');continue
    await go(path[0],130,cup,False,'approach '+source);await go(path[0],100,cup,False,'descend '+source);await jaw(False)
    for xy in path[1:]:await go(xy,100,cup,True,'slide to '+target)
    await jaw(True);occupied[target]=occupied.pop(source);report['cup_transfers_completed']+=1;report['occupied']=dict(occupied);event('transfer_finished',source=source,destination=target)
    await go(path[-1],130,cup,False,'retreat '+target)
   report['completed']=True;report['arm_is_moving']=await arm.is_moving();report['gripper_position']=(await grip.do_command({'get':True},timeout=3)).get('pos',0);report['gripper_fully_open']=report['gripper_position']>=840;report['release_position_confirmed']=report['gripper_position']>=open_position-10;report['final_cup_positions_visually_verified']=False;event('finished')
  except Exception as exc:
   report['error']=str(exc)
   try:report['stop_sent']=False;report['arm_is_moving']=await arm.is_moving();report['holding_at_stop']=await held()
   except Exception:report['stop_sent']=False
   save();print('STOPPED:',str(exc),flush=True)
if __name__=='__main__':asyncio.run(main())
