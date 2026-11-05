"""SQLAlchemy 모델 집합."""
from app.models.card_publication import CardProgramPublication
from app.models.card_benefit_version import CardBenefitVersion, CardBenefitFieldEvidence, CardBenefitExtraction
from app.models.card_collection import (
    CardIssuer, CardCollectionSource, CardProductSource, CardSourceSnapshot,
    CardCollectionRun, CardCollectionAttempt,
)
from app.models.scholarship import Scholarship, ScholarshipEmbedding
from app.models.source_document import SourceDocument, SourceFetchAttempt
from app.models.scholarship_revision import ScholarshipRevision
from app.models.source_retry import SourceRetryJob
from app.models.writing_session import WritingSession
from app.models.application import (
    ApplicationDocument,
    DocumentEmbedding,
    UserApplication,
)
from app.models.crawl_run import CrawlRun, CrawlRunStatus
from app.models.store import SpendCategory, PayMethod, Store, StoreCategory, StoreOffer
from app.models.card import (
    BenefitType,
    Card,
    CardBenefit,
    Confidence,
    PeriodType,
)
from app.models.user import User
from app.models.auth_session import AuthSession
from app.models.password_reset import PasswordResetToken
from app.models.community import (
    Board,
    BoardCategory,
    BoardRead,
    Comment,
    CommentLike,
    Post,
    PostLike,
    Report,
    Scrap,
)
from app.models.local_benefit import LocalBenefitMerchant
from app.models.saving import SavingKind, SavingRecord, UserBenefitCheck

from app.models.map_source import MapSourceState

__all__ = [
    "MapSourceState",
    # 장학금 챗봇
    "Scholarship",
    "ScholarshipEmbedding",
    "UserApplication",
    "ApplicationDocument",
    "DocumentEmbedding",
    "CrawlRun",
    "CrawlRunStatus",
    # TMI 지도
    "Store",
    "StoreOffer",
    "StoreCategory",
    "SpendCategory",
    "PayMethod",
    "Card",
    "CardBenefit",
    "BenefitType",
    "Confidence",
    "PeriodType",
    "LocalBenefitMerchant",
    "SavingRecord",
    "UserBenefitCheck",
    "SavingKind",
    # 공통
    "User",
    "AuthSession",
    "PasswordResetToken",
    "Board",
    "BoardCategory",
    "BoardRead",
    "Comment",
    "CommentLike",
    "Post",
    "PostLike",
    "Report",
    "Scrap",
]
from app.models.map_verification import MapEvidence, MapLocation, MapJobLease, MapReviewEvent
from app.models.card_document_text import CardDocumentText
from app.models.card_document_import import CardDocumentImport
from app.models.issuer_merchant_capture import IssuerMerchantCapture
from app.models.information_session import InformationSession
