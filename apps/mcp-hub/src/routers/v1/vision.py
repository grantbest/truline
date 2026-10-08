from fastapi import APIRouter
from pydantic import BaseModel, Field
from access_auth import check_scope
from tools.vision import (
    extract_and_stage,
    extract_from_image,
    StagedVisionExtraction,
    VisionExtraction,
)

router = APIRouter()

class VisionRequest(BaseModel):
    image_base64: str = Field(..., description="Base64 encoded image (JPEG/PNG)")

@router.post(
    "/extract",
    summary="Extract structured data from a photo and stage it for review",
    operation_id="vision_extract",
)
async def vision_extract(req: VisionRequest) -> StagedVisionExtraction:
    check_scope("vision.write")
    return await extract_and_stage(req.image_base64)

@router.post(
    "/extract_only",
    summary="Extract structured data from a photo (no persistence) — for dry-runs",
    operation_id="vision_extract_only",
)
async def vision_extract_only(req: VisionRequest) -> VisionExtraction:
    check_scope("vision.write")
    return await extract_from_image(req.image_base64)
