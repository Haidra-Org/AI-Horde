import uuid
from datetime import datetime

from sqlalchemy.dialects.postgresql import UUID

from horde.flask import SQLITE_MODE, db
from horde.utils import get_expiry_date

uuid_column_type = lambda: UUID(as_uuid=True) if not SQLITE_MODE else db.String(36)  # FIXME # noqa E731


class UploadedImage(db.Model):
    """For storing the upload and download links for an image"""

    __tablename__ = "uploaded_images"
    id = db.Column(uuid_column_type(), primary_key=True, default=uuid.uuid4)  # Then move to this
    upload_url = db.Column(db.Text, nullable=False)
    download_ul = db.Column(db.Text, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"))
    user = db.relationship("User", back_populates="uploaded_images")
    expiry = db.Column(db.DateTime, default=get_expiry_date, index=True)
    created = db.Column(db.DateTime(timezone=False), default=datetime.utcnow, index=True)


# TODO: Rest of this class which will be used to allow the horde to provide an upload link for the user and a download link to the worker
# Or maybe receive the image as b64 and then upload it myself
