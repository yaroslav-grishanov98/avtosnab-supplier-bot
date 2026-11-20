from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ParsedSupplierProductRow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    row_number: int = Field(description="Original source row number.", ge=1)
    raw_name: str = Field(description="Original supplier product name or source row text.", min_length=1)
    normalized_name: str = Field(description="Clean normalized product name.", min_length=1)
    price: Optional[Decimal] = Field(default=None, description="Unit price as a number without currency.", ge=0)
    currency: str = Field(default="RUB", description="ISO currency code.")
    unit_code: str = Field(default="", description="Sale unit code from the active Unit reference table.")
    package_info: Optional[str] = Field(default=None, description="Package, size, weight, volume or box ratio.")
    brand: Optional[str] = Field(default=None, description="Brand or manufacturer if present.")
    volume: Optional[Decimal] = Field(default=None, description="Volume value.", ge=0)
    volume_unit: Optional[str] = Field(default=None, description="Volume unit code, usually л or мл.")
    weight: Optional[Decimal] = Field(default=None, description="Weight value.", ge=0)
    weight_unit: Optional[str] = Field(default=None, description="Weight unit code, usually кг or г.")
    min_order_qty: Optional[Decimal] = Field(default=None, description="Minimum order quantity.", ge=0)
    multiplicity: Optional[Decimal] = Field(default=None, description="Delivery multiple or items per box.", ge=0)
    supplier_sku: Optional[str] = Field(default=None, description="Supplier article, code or SKU.")
    category_path: list[str] = Field(default_factory=list, description="Supplier category path if present.")
    availability_status: str = Field(default="available", description="available, unavailable, inactive or archived.")
    confidence: Decimal = Field(default=Decimal("0.9500"), description="Parser confidence from 0 to 1.", ge=0, le=1)
    comment: Optional[str] = Field(default=None, description="Short parser note.")
    warnings: list[str] = Field(default_factory=list, description="Validation or extraction warnings.")

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value):
        value = (value or "RUB").strip().upper()
        return value[:3] or "RUB"

    @field_validator("unit_code", "volume_unit", "weight_unit", mode="before")
    @classmethod
    def normalize_optional_text(cls, value):
        return "" if value is None else str(value).strip()


class ParsedSupplierCatalog(BaseModel):
    model_config = ConfigDict(extra="ignore")

    products: list[ParsedSupplierProductRow] = Field(default_factory=list)
