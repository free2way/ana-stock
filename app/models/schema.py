from pydantic import BaseModel, ConfigDict


class SymbolCreate(BaseModel):
    ticker: str
    name: str | None = None
    market: str | None = None
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None


class SymbolRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ticker: str
    name: str | None = None
    market: str | None = None
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    is_active: int
    created_at: str
    updated_at: str


class PriceSyncStateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol_id: int
    provider: str
    last_synced_date: str | None = None
    status: str | None = None
    message: str | None = None
    updated_at: str
