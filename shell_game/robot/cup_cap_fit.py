"""Fit the visible flat cup bottom, allowing it to tilt relative to the table."""
import numpy as np
import cv2

def fit_cap(cloud, heights):
    candidates=cloud[heights>np.percentile(heights,40)]
    sample=candidates[::max(1,len(candidates)//2500)]
    rng=np.random.default_rng(17)
    best=None;count=0
    for _ in range(200):
        tri=sample[rng.choice(len(sample),3,replace=False)]
        n=np.cross(tri[1]-tri[0],tri[2]-tri[0]);length=np.linalg.norm(n)
        if length<1:continue
        n/=length
        if abs(n[2])<.75:continue
        d=-n@tri[0];inside=np.abs(sample@n+d)<1.2
        if inside.sum()>count:best=(n,d);count=inside.sum()
    if best is None:raise ValueError('No cap plane')
    n,d=best
    surface=candidates[np.abs(candidates@n+d)<1.5]
    origin=np.median(surface,axis=0)
    _,_,vt=np.linalg.svd(surface-origin,full_matrices=False)
    basis=vt[:2].T
    coords=(surface-origin)@basis
    hull=cv2.convexHull(coords.astype(np.float32)).reshape(-1,2).astype(float)
    c=np.linalg.lstsq(np.column_stack([2*hull,np.ones(len(hull))]),(hull*hull).sum(1),rcond=None)[0]
    radius=float(np.sqrt(max(0,c[2]+c[0]**2+c[1]**2)))
    rms=float(np.sqrt(np.mean((np.linalg.norm(hull-c[:2],axis=1)-radius)**2)))
    center=origin+basis@c[:2]
    return {'center_world_mm':center.tolist(),'radius_mm':radius,'boundary_rms_mm':rms,'plane_rms_mm':float(np.sqrt(np.mean(((surface-origin)@vt[2])**2))),'points':len(surface)}
