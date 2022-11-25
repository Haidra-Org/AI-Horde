
from horde.classes import db

def find_workers_by_user(user):
    from horde.classes import User
    return db.session.query(User).filter(user_id=user.id).all()
