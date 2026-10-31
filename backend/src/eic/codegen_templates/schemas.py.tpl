"""__LABEL__ CRUD 输入；租户与权限由服务端决定。"""
from pydantic import BaseModel, ConfigDict, Field


class PageQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    pageNum: int = Field(default=1, ge=1, le=1000000)
    pageSize: int = Field(default=20, ge=1, le=100)
    searchKey: str = Field(default="", max_length=200)


class SaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: int | None = Field(default=None, ge=1, le=9223372036854775807)
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    active: bool = Field(default=True, strict=True)
__PARENT_SCHEMA__
