"""Offline point-cloud analysis; outputs are unvalidated motion estimates."""
import json,math,sys
from pathlib import Path
from collections import defaultdict,deque
import numpy as np
from cup_cap_fit import fit_cap
root=Path(sys.argv[1]) if len(sys.argv)>1 else Path(__file__).parent/'stereo-scan';raw=(root/'cloud.pcd').read_bytes();offset=raw.index(b'DATA binary\n')+len(b'DATA binary\n');a=np.frombuffer(raw,offset=offset,dtype=[('x','<f4'),('y','<f4'),('z','<f4'),('rgb','<u4')]);meta=json.loads((root/'capture.json').read_text());rotation=np.array(meta['basis_world_columns']).T;origin=np.array([meta['camera_world'][k] for k in ('x','y','z')]);xyz=np.column_stack([a[k] for k in ('x','y','z')])*1000;rgb=a['rgb'];valid=np.isfinite(xyz).all(1)&(xyz[:,2]>250)&(xyz[:,2]<1500);world=xyz@rotation.T+origin;valid&=(world[:,0]>-100)&(world[:,0]<700)&(world[:,1]>-850)&(world[:,1]<-250)&(world[:,2]>-80)&(world[:,2]<180);world=world[valid];rgb=rgb[valid];r=(rgb>>16)&255;g=(rgb>>8)&255;b=rgb&255;blue=(b>r*1.3)&(b>g*1.05)&(b>35)
# Fit native 3D ground geometry without relying on tape color or exposure.
rng=np.random.default_rng(42);possible=world[(world[:,2]>-60)&(world[:,2]<45)];sample=possible[::max(1,len(possible)//12000)];best=None;best_count=0
for _ in range(160):
 tri=sample[rng.choice(len(sample),3,replace=False)];normal=np.cross(tri[1]-tri[0],tri[2]-tri[0]);length=np.linalg.norm(normal)
 if length<1:continue
 normal/=length
 if abs(normal[2])<.95:continue
 d=-normal@tri[0];inside=np.abs(sample@normal+d)<3;count=int(inside.sum())
 if count>best_count:best_count=count;best=(normal,d)
if best is None or best_count<len(sample)*.5:raise RuntimeError('No sufficiently supported surface plane')
normal,d=best;ground=np.abs(possible@normal+d)<3;tape=possible[ground][::10];mask=np.ones(len(tape),dtype=bool)
for _ in range(5):
 coeff=np.linalg.lstsq(np.column_stack([tape[mask,:2],np.ones(mask.sum())]),tape[mask,2],rcond=None)[0];residual=tape[:,2]-(np.column_stack([tape[:,:2],np.ones(len(tape))])@coeff);mask=np.abs(residual)<3
heights=world[:,2]-(np.column_stack([world[:,:2],np.ones(len(world))])@coeff);obj=(heights>25)&(heights<120)&~blue
points=world[obj];heights=heights[obj];vox=defaultdict(list)
for i,key in enumerate(np.floor(points/8).astype(int)):vox[tuple(key)].append(i)
seen=set();clusters=[]
for start in vox:
 if start in seen:continue
 seen.add(start);queue=deque([start]);indices=[]
 while queue:
  k=queue.popleft();indices.extend(vox[k])
  for dx in (-1,0,1):
   for dy in (-1,0,1):
    for dz in (-1,0,1):
     q=(k[0]+dx,k[1]+dy,k[2]+dz)
     if q in vox and q not in seen:seen.add(q);queue.append(q)
 if len(indices)<100:continue
 cloud=points[indices];hi=heights[indices];lo,up=np.percentile(cloud,[2,98],axis=0);top=cloud[hi>np.percentile(hi,98)-4];center=np.median(top,axis=0)
 xy=np.unique(np.round(top[:,:2],1),axis=0);xy=sorted(map(tuple,xy.tolist()))
 def cross(o,a,b):return (a[0]-o[0])*(b[1]-o[1])-(a[1]-o[1])*(b[0]-o[0])
 lower=[]
 for t in xy:
  while len(lower)>=2 and cross(lower[-2],lower[-1],t)<=0:lower.pop()
  lower.append(t)
 upper=[]
 for t in reversed(xy):
  while len(upper)>=2 and cross(upper[-2],upper[-1],t)<=0:upper.pop()
  upper.append(t)
 hull=np.array(lower[:-1]+upper[:-1]);circle=np.linalg.lstsq(np.column_stack([2*hull,np.ones(len(hull))]),(hull*hull).sum(1),rcond=None)[0];radius=float(np.sqrt(max(0,circle[2]+circle[0]**2+circle[1]**2)));circle_rms=float(np.sqrt(np.mean((np.linalg.norm(hull-circle[:2],axis=1)-radius)**2)));center[:2]=circle[:2]
 cap=fit_cap(cloud,hi) if len(indices)>1500 else None
 clusters.append({'cap_fit':cap,'points':len(indices),'top_circle_radius_mm':radius,'top_circle_fit_rms_mm':circle_rms,'top_center_world_mm':center.tolist(),'bounds_2_98_mm':[lo.tolist(),up.tolist()],'height_above_tape_5_95_mm':np.percentile(hi,[5,95]).tolist()})
report={'native_pointcloud':True,'total_points':len(a),'valid_workspace_points':len(world),'table_geometry_samples':len(tape),'table_fit_method':'RANSAC on native 3D geometry; independent of tape color','table_plane_world_z_eq_ax_by_c':coeff.tolist(),'table_inlier_fraction':float(mask.mean()),'table_rms_mm':float(np.sqrt(np.mean(residual[mask]**2))),'object_clusters':sorted(clusters,key=lambda c:c['top_center_world_mm'][0]),'validated_for_motion':False,'note':'Single view; cross-view consistency and cup identity/geometry verification remain required.'}
(root/'automatic-analysis.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))
