"""P0-2: presigned PUT в объектное хранилище.

Фото гипотез и сканы согласий фронт больше не кладёт ссылкой «откуда-нибудь»:
он получает сюда URL, заливает файл сам, в API уходит уже public_url.
Офлайн-очередь переживает 404 — ручка обязана существовать.
"""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath

import boto3
from botocore.config import Config as BotoConfig
from fastapi import APIRouter, Depends, HTTPException, status

from app.auth import get_current_user
from app.config import settings
from app.models import User
from app.schemas import UploadPresignOut, UploadPresignRequest

router = APIRouter(prefix="/api/v1/uploads", tags=["uploads"])

_ALLOWED_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "application/pdf",
}
_ALLOWED_PURPOSES = {
    "hypothesis_photo",
    "consent_scan",
    "certificate",
    "event_photo",
    "other",
}


def _extension(filename: str) -> str:
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"}:
        return suffix
    return ""


@router.post(
    "/presign",
    response_model=UploadPresignOut,
    summary="Выдать URL для прямой загрузки файла (P0-2)",
)
async def presign_upload(
    body: UploadPresignRequest,
    user: User = Depends(get_current_user),
):
    content_type = body.content_type.lower().strip()
    if content_type not in _ALLOWED_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported content type: {body.content_type}",
        )
    purpose = body.purpose.strip().lower()
    if purpose not in _ALLOWED_PURPOSES:
        purpose = "other"

    key = f"{purpose}/{user.id}/{uuid.uuid4().hex}{_extension(body.filename)}"
    expires = settings.UPLOAD_PRESIGN_EXPIRE_SECONDS
    # Подписываем на публичный хост: браузер не резолвит docker-имя minio.
    endpoint = settings.S3_PUBLIC_URL or settings.S3_ENDPOINT_URL
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=settings.S3_ACCESS_KEY,
        aws_secret_access_key=settings.S3_SECRET_KEY,
        region_name="us-east-1",
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    upload_url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.S3_BUCKET,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=expires,
    )
    public_url = f"{endpoint.rstrip('/')}/{settings.S3_BUCKET}/{key}"
    return UploadPresignOut(
        upload_url=upload_url,
        public_url=public_url,
        headers={"Content-Type": content_type},
        expires_in=expires,
        key=key,
    )
