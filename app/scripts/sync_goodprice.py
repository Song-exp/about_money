"""착한가격업소 공공데이터를 한 번 적재한다."""
import json

from app.core.db import SessionLocal
from app.services.goodprice import sync_goodprice


def main():
    with SessionLocal() as db:
        print(json.dumps(sync_goodprice(db), ensure_ascii=False))


if __name__ == '__main__':
    main()
