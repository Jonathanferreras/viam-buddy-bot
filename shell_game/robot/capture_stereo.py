"""Read-only native stereo capture. Does not move the arm or gripper."""
import asyncio,os,json
from pathlib import Path
from dotenv import load_dotenv
from viam.robot.client import RobotClient
from viam.components.arm import Arm
from viam.components.camera import Camera
from viam.proto.common import Pose,PoseInFrame
async def main():
 root=Path(__file__).parent;load_dotenv(root/'.env')
 robot=await asyncio.wait_for(RobotClient.at_address(os.environ['VIAM_MACHINE_ADDRESS'],RobotClient.Options.with_api_key(api_key=os.environ['VIAM_API_KEY'],api_key_id=os.environ['VIAM_API_KEY_ID'])),30)
 async with robot:
  arm=Arm.from_robot(robot,'arm')
  if await arm.is_moving():raise RuntimeError('Arm moving; no scan')
  folder=root/'stereo-scan';folder.mkdir(exist_ok=True)
  camera=Camera.from_robot(robot,'cam')
  frames,_=await camera.get_images(timeout=10)
  for i,f in enumerate(frames):
   if 'jpeg' in str(f.mime_type):(folder/f'color-{i}.jpg').write_bytes(f.data)
  cloud,mime=await camera.get_point_cloud(timeout=20)
  (folder/'cloud.pcd').write_bytes(cloud)
  origin=(await robot.transform_pose(PoseInFrame(reference_frame='cam',pose=Pose(o_z=1)),'world')).pose
  basis=[]
  for xyz in ((1,0,0),(0,1,0),(0,0,1)):
   b=(await robot.transform_pose(PoseInFrame(reference_frame='cam',pose=Pose(x=xyz[0],y=xyz[1],z=xyz[2],o_z=1)),'world')).pose
   basis.append([b.x-origin.x,b.y-origin.y,b.z-origin.z])
  meta={'basis_world_columns':basis,'mime':mime,'camera_world':{k:getattr(origin,k) for k in ('x','y','z','o_x','o_y','o_z','theta')},'bytes':len(cloud)}
  (folder/'capture.json').write_text(json.dumps(meta,indent=2));print(json.dumps(meta,indent=2));print(cloud[:400])
asyncio.run(main())
