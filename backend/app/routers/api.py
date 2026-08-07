from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select
from pydantic import BaseModel
from app.core.db import get_db
from app.models.db import Dataset, ArchiveRun, AuditEvent
from app.services.temperature import TemperatureService
from app.services.evidence import EvidenceService
from app.services.simulation import SimulationService
from app.services.archive import ArchiveService
from app.services.datahub import DataHubService

router = APIRouter(prefix="/api")
archive_service = ArchiveService()

class Cutoff(BaseModel): cutoff_date: str
class Execute(Cutoff): approved_by: str = "hackathon-judge"
class Restore(BaseModel): run_id: int; temporary: bool = True

def audit(db, typ, urn, detail):
    db.add(AuditEvent(event_type=typ,dataset_urn=urn,detail=detail)); db.commit()

@router.get('/health')
def health(): return {"ok":True,"service":"ColdLineage"}

@router.get('/datasets')
def datasets(db: Session=Depends(get_db)):
    out=[]
    for ds in db.scalars(select(Dataset)).all():
        ev=EvidenceService.build(ds)
        out.append({"urn":ds.urn,"name":ds.name,"owner":ds.owner,"domain":ds.domain,"rows":ds.rows,"size_gb":ds.size_gb,"pii":ds.pii,"phi":ds.phi,**ev})
    return out

@router.get('/datasets/{dataset_id}')
def dataset(dataset_id:int, db:Session=Depends(get_db)):
    ds=db.get(Dataset,dataset_id)
    if not ds: raise HTTPException(404,'dataset not found')
    ev=EvidenceService.build(ds)
    return {"id":ds.id,"urn":ds.urn,"name":ds.name,"owner":ds.owner,"domain":ds.domain,"rows":ds.rows,"size_gb":ds.size_gb,"metadata":ds.metadata_json,**ev}

@router.post('/datasets/{dataset_id}/preview')
def preview(dataset_id:int, body:Cutoff, db:Session=Depends(get_db)):
    ds=db.get(Dataset,dataset_id)
    if not ds: raise HTTPException(404,'dataset not found')
    result=archive_service.preview(db,ds,body.cutoff_date); audit(db,'ARCHIVE_PREVIEW',ds.urn,result)
    return result

@router.post('/datasets/{dataset_id}/simulate')
def simulate(dataset_id:int, body:Cutoff, db:Session=Depends(get_db)):
    ds=db.get(Dataset,dataset_id)
    if not ds: raise HTTPException(404,'dataset not found')
    result=SimulationService.simulate(ds,body.cutoff_date); audit(db,'SIMULATION',ds.urn,result)
    return result

@router.post('/datasets/{dataset_id}/execute')
async def execute(dataset_id:int, body:Execute, db:Session=Depends(get_db)):
    ds=db.get(Dataset,dataset_id)
    if not ds: raise HTTPException(404,'dataset not found')
    ev=EvidenceService.build(ds)
    if not ev['eligible']: raise HTTPException(409,{"message":"Policy engine blocked archive","evidence":ev})
    sim=SimulationService.simulate(ds,body.cutoff_date)
    if sim['recommendation']=='DO_NOT_ARCHIVE': raise HTTPException(409,sim)
    try: manifest=archive_service.execute(db,ds,body.cutoff_date)
    except Exception as e: raise HTTPException(500,str(e))
    run=ArchiveRun(dataset_urn=ds.urn,cutoff_date=body.cutoff_date,status='VERIFIED',rows_archived=manifest['rows'],bytes_archived=manifest['bytes'],object_uri=manifest['object_uri'],checksum=manifest['sha256'],manifest=manifest,approved_by=body.approved_by,completed_at=datetime.utcnow())
    db.add(run); db.commit(); db.refresh(run)
    writeback=await DataHubService().writeback(ds.urn,manifest)
    audit(db,'ARCHIVE_EXECUTED',ds.urn,{"run_id":run.id,"manifest":manifest,"datahub":writeback})
    return {"run_id":run.id,"manifest":manifest,"datahub":writeback}

@router.post('/restore')
def restore(body:Restore, db:Session=Depends(get_db)):
    run=db.get(ArchiveRun,body.run_id)
    if not run: raise HTTPException(404,'archive run not found')
    ds=db.scalar(select(Dataset).where(Dataset.urn==run.dataset_urn))
    try: result=archive_service.restore(db,ds,run.object_uri,body.temporary)
    except Exception as e: raise HTTPException(500,str(e))
    audit(db,'RESTORE_COMPLETED',ds.urn,{"run_id":run.id,**result})
    return result

@router.get('/runs')
def runs(db:Session=Depends(get_db)):
    return [{"id":r.id,"dataset_urn":r.dataset_urn,"cutoff_date":r.cutoff_date,"status":r.status,"rows_archived":r.rows_archived,"object_uri":r.object_uri,"checksum":r.checksum,"approved_by":r.approved_by,"created_at":r.created_at} for r in db.scalars(select(ArchiveRun).order_by(ArchiveRun.id.desc())).all()]

@router.get('/audit')
def audit_events(db:Session=Depends(get_db)):
    return [{"id":a.id,"event_type":a.event_type,"dataset_urn":a.dataset_urn,"actor":a.actor,"detail":a.detail,"created_at":a.created_at} for a in db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(100)).all()]
