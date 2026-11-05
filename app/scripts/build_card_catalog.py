"""수집한 상품 후보로 카드 목록과 이미지 주소를 채운다."""
import json

from app.core.db import SessionLocal
from app.services.card_catalog import build_catalog


def main():
    with SessionLocal() as db:
        print(json.dumps(build_catalog(db), ensure_ascii=False))


if __name__ == '__main__':
    main()
