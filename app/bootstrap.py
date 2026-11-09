from sqlalchemy import select
from .db import Session, Staff
from .config import settings
from pwdlib import PasswordHash


def main():
    with Session.begin() as s:
        if not s.scalar(select(Staff).where(Staff.role == "admin")):
            if len(settings.admin_password) < 12:
                raise RuntimeError("Set ADMIN_PASSWORD to at least 12 characters before first start")
            s.add(Staff(username=settings.admin_username, password_hash=PasswordHash.recommended().hash(settings.admin_password), role="admin"))


if __name__ == "__main__":
    main()
