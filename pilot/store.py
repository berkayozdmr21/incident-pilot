import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from sqlalchemy import create_engine,Column,String,Float,Integer,JSON,Text
from sqlalchemy.orm import declarative_base,sessionmaker

ROOT=Path(os.path.abspath(__file__)).parents[1]
DATA=Path(os.getenv('PILOT_DATA_DIR',str(ROOT/'data')))
DATA.mkdir(parents=True,exist_ok=True)
DATABASE_URL=os.getenv('DATABASE_URL','sqlite:///'+str(DATA/'pilot.sqlite3'))
engine=create_engine(DATABASE_URL,connect_args={'check_same_thread':False,'timeout':20} if DATABASE_URL.startswith('sqlite') else {},pool_pre_ping=True)
Session=sessionmaker(bind=engine,expire_on_commit=False)
Base=declarative_base()


class Incident(Base):
    __tablename__='incidents'
    id=Column(String,primary_key=True,default=lambda:uuid.uuid4().hex)
    service=Column(String,nullable=False)
    active_service=Column(String,unique=True,nullable=True)
    status=Column(String,default='open')
    severity=Column(String,default='warning')
    title=Column(String)
    opened_at=Column(Float,default=time.time)
    resolved_at=Column(Float,nullable=True)
    analysis=Column(JSON,nullable=True)
    evidence=Column(JSON,default=list)
    revision=Column(Integer,default=1)
    healthy_streak=Column(Integer,default=0)


class Job(Base):
    __tablename__='jobs'
    id=Column(String,primary_key=True,default=lambda:uuid.uuid4().hex)
    incident_id=Column(String,index=True)
    status=Column(String,default='queued',index=True)
    created_at=Column(Float,default=time.time)
    updated_at=Column(Float,default=time.time)
    attempts=Column(Integer,default=0)
    error=Column(Text,nullable=True)


class Audit(Base):
    __tablename__='audit'
    id=Column(String,primary_key=True,default=lambda:uuid.uuid4().hex)
    time=Column(Float,default=time.time,index=True)
    incident_id=Column(String,nullable=True)
    kind=Column(String)
    message=Column(Text)
    details=Column(JSON,default=dict)


class Snapshot(Base):
    __tablename__='snapshots'
    id=Column(String,primary_key=True,default=lambda:uuid.uuid4().hex)
    time=Column(Float,default=time.time,index=True)
    services=Column(JSON)


def init_db(): Base.metadata.create_all(engine)


@contextmanager
def session():
    db=Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback();raise
    finally: db.close()


def incident_dict(item):
    return {key:getattr(item,key) for key in ['id','service','status','severity','title','opened_at','resolved_at','analysis','evidence','revision','healthy_streak']}


def audit(db,kind,message,incident_id=None,details=None):
    db.add(Audit(kind=kind,message=message,incident_id=incident_id,details=details or {}))
