import os, random
from datetime import datetime, timedelta
from faker import Faker
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
from app.core.db import Base
from app.models.db import Dataset

URL=os.getenv('DATABASE_URL','postgresql+psycopg://coldlineage:coldlineage@localhost:5433/coldlineage')
engine=create_engine(URL)
Base.metadata.create_all(engine)
f=Faker(); random.seed(7); Faker.seed(7)

def make_table(name, start_year, count, phi=False):
    with engine.begin() as c:
        c.execute(text(f'DROP TABLE IF EXISTS "{name}"'))
        c.execute(text(f'''CREATE TABLE "{name}" (
            id BIGSERIAL PRIMARY KEY,
            patient_id TEXT,
            event_date DATE NOT NULL,
            encounter_type TEXT,
            amount NUMERIC(12,2),
            diagnosis_code TEXT,
            region TEXT
        )'''))
        rows=[]
        for _ in range(count):
            d=datetime(start_year,1,1)+timedelta(days=random.randint(0, 365*(2026-start_year)+180))
            rows.append({'patient':f'P-{random.randint(1,4000):05d}','date':d.date(),'etype':random.choice(['inpatient','outpatient','telehealth','emergency']),'amount':round(random.uniform(80,9500),2),'dx':random.choice(['I10','E11.9','J45.2','M54.5','Z00.0']),'region':random.choice(['WEST','EAST','MIDWEST','SOUTH'])})
        c.execute(text(f'''INSERT INTO "{name}" (patient_id,event_date,encounter_type,amount,diagnosis_code,region)
            VALUES (:patient,:date,:etype,:amount,:dx,:region)'''), rows)

make_table('patient_encounters',2021,55000,True)
make_table('claims_history',2020,38000,True)
make_table('care_events_live',2025,12000,True)

with Session(engine) as s:
    s.query(Dataset).delete()
    now=datetime.utcnow()
    datasets=[
      Dataset(urn='urn:li:dataset:(urn:li:dataPlatform:postgres,patient_encounters,PROD)',name='patient_encounters',domain='Clinical Analytics',owner='Maya Chen',rows=55000,size_gb=8.7,date_column='event_date',phi=True,retention_years=2,last_query_at=now-timedelta(days=418),query_count_90d=1,downstream_active=2,business_criticality=.35,metadata_json={'dependencies':[{'name':'quarterly_compliance_dashboard','type':'dashboard','history_years':1,'reason':'Queries trailing 12 months only'},{'name':'patient_ltv_model','type':'ml_model','history_years':2,'reason':'Training window is 24 months'}]}),
      Dataset(urn='urn:li:dataset:(urn:li:dataPlatform:postgres,claims_history,PROD)',name='claims_history',domain='Finance & Claims',owner='Evan Brooks',rows=38000,size_gb=13.2,date_column='event_date',phi=True,retention_years=7,legal_hold=True,last_query_at=now-timedelta(days=530),query_count_90d=0,downstream_active=1,business_criticality=.8,metadata_json={'dependencies':[{'name':'annual_audit_extract','type':'pipeline','history_years':7,'reason':'Regulatory audit requires seven years'}]}),
      Dataset(urn='urn:li:dataset:(urn:li:dataPlatform:postgres,care_events_live,PROD)',name='care_events_live',domain='Clinical Operations',owner='Priya Shah',rows=12000,size_gb=4.1,date_column='event_date',phi=True,retention_years=2,last_query_at=now-timedelta(days=2),query_count_90d=820,downstream_active=4,business_criticality=.95,metadata_json={'dependencies':[{'name':'operations_dashboard','type':'dashboard','history_years':1,'reason':'Actively queried'},{'name':'risk_model_v4','type':'ml_model','history_years':2,'reason':'Production model feature source'}]})
    ]
    s.add_all(datasets);s.commit()
print('Seeded ColdLineage demo: 3 datasets, 105,000 synthetic healthcare rows.')
