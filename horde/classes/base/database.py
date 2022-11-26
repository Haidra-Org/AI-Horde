
from horde.classes import db

def find_workers_by_user(user):
    from horde.classes import Worker
    return db.session.query(Worker).filter(user_id=user.id).all()

def find_workers_by_team(team):
    from horde.classes import Worker
    return db.session.query(Worker).filter(user_id=team.owner_id).all()

def find_team_by_worker(worker):
    from horde.classes import Team
    return db.session.query(Team).filter(id=worker.team_id).all()