from datetime import datetime
from typing import Literal, Optional, List
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import RoleEnum, PaymentMethodEnum, InvoiceStatusEnum


# ---------- AUTH ----------
class UserCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)
    role: RoleEnum = RoleEnum.staff


class InitialAdminCreate(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    username: str
    role: RoleEnum
    is_active: bool


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class LoginRequest(BaseModel):
    username: str
    password: str


# ---------- PRODUCTS ----------
class ProductBase(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=1000)
    catalog_serial: Optional[int] = Field(default=None, ge=1)
    brand: Optional[str] = Field(default=None, max_length=120)
    sku: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    unit: str = Field(min_length=1, max_length=40)
    rate: Optional[Decimal] = Field(default=None, ge=0)
    selling_price: Optional[Decimal] = Field(default=None, ge=0)
    purchase_price_including_gst: Optional[Decimal] = Field(default=None, ge=0)
    purchase_price_excluding_gst: Optional[Decimal] = Field(default=None, ge=0)
    purchase_gst_component: Optional[Decimal] = Field(default=None, ge=0)
    gst_rate: Optional[Decimal] = Field(default=None, ge=0, le=100)
    gst_selection_type: Optional[Literal['preset', 'custom']] = None
    pack_size: Optional[str] = Field(default=None, max_length=80)
    initial_quantity: Optional[Decimal] = Field(default=None, ge=0)
    selling_price_tax_mode: Literal['inclusive', 'exclusive'] = 'exclusive'
    hsn_code: Optional[str] = Field(default=None, max_length=20)
    discount_percentage: Optional[Decimal] = Field(default=None, ge=0, le=100)
    stock: float = Field(default=0, ge=0)
    min_stock: float = Field(default=10, ge=0)
    expires_on: Optional[datetime] = None
    supplier: Optional[str] = Field(default=None, max_length=160)
    is_active: bool = True

    @model_validator(mode='before')
    @classmethod
    def sync_legacy_rate(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            if 'selling_price' not in value and 'rate' in value:
                value['selling_price'] = value['rate']
            elif 'rate' not in value and 'selling_price' in value:
                value['rate'] = value['selling_price']
        return value

    @model_validator(mode='after')
    def validate_gst_selection(self):
        if (self.gst_selection_type == 'preset' and self.gst_rate is not None
                and self.gst_rate not in {Decimal('0'), Decimal('5'), Decimal('12'), Decimal('18'), Decimal('28')}):
            raise ValueError('Preset GST must be 0%, 5%, 12%, 18%, or 28%; select Custom GST for other rates')
        return self


class ProductCreate(ProductBase):
    pass


class ProductUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    description: Optional[str] = Field(default=None, max_length=1000)
    catalog_serial: Optional[int] = Field(default=None, ge=1)
    brand: Optional[str] = Field(default=None, max_length=120)
    sku: Optional[str] = Field(default=None, min_length=1, max_length=80)
    category: Optional[str] = Field(default=None, min_length=1, max_length=80)
    unit: Optional[str] = Field(default=None, min_length=1, max_length=40)
    rate: Optional[Decimal] = Field(default=None, ge=0)
    selling_price: Optional[Decimal] = Field(default=None, ge=0)
    purchase_price_including_gst: Optional[Decimal] = Field(default=None, ge=0)
    purchase_price_excluding_gst: Optional[Decimal] = Field(default=None, ge=0)
    purchase_gst_component: Optional[Decimal] = Field(default=None, ge=0)
    gst_rate: Optional[Decimal] = Field(default=None, ge=0, le=100)
    gst_selection_type: Optional[Literal['preset', 'custom']] = None
    pack_size: Optional[str] = Field(default=None, max_length=80)
    initial_quantity: Optional[Decimal] = Field(default=None, ge=0)
    selling_price_tax_mode: Optional[Literal['inclusive', 'exclusive']] = None
    hsn_code: Optional[str] = Field(default=None, max_length=20)
    discount_percentage: Optional[Decimal] = Field(default=None, ge=0, le=100)
    stock: Optional[float] = Field(default=None, ge=0)
    min_stock: Optional[float] = Field(default=None, ge=0)
    expires_on: Optional[datetime] = None
    supplier: Optional[str] = Field(default=None, max_length=160)
    is_active: Optional[bool] = None

    @model_validator(mode='before')
    @classmethod
    def sync_legacy_rate(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            if 'selling_price' not in value and 'rate' in value:
                value['selling_price'] = value['rate']
            elif 'rate' not in value and 'selling_price' in value:
                value['rate'] = value['selling_price']
        return value

    @model_validator(mode='after')
    def validate_gst_selection(self):
        if (self.gst_selection_type == 'preset' and self.gst_rate is not None
                and self.gst_rate not in {Decimal('0'), Decimal('5'), Decimal('12'), Decimal('18'), Decimal('28')}):
            raise ValueError('Preset GST must be 0%, 5%, 12%, 18%, or 28%; select Custom GST for other rates')
        return self


class ProductOut(BaseModel):
    """Customer/staff-safe product view; purchase costs are intentionally absent."""
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    description: Optional[str] = None
    catalog_serial: Optional[int] = None
    brand: Optional[str] = None
    sku: str
    category: str
    unit: str
    rate: Optional[Decimal]
    selling_price: Optional[Decimal]
    stock: float
    min_stock: float
    expires_on: Optional[datetime] = None
    supplier: Optional[str] = None
    is_active: bool
    pack_size: Optional[str] = None
    gst_rate: Optional[Decimal] = None
    gst_selection_type: Optional[str] = None
    selling_price_tax_mode: Optional[str] = None
    hsn_code: Optional[str] = None
    discount_percentage: Optional[Decimal] = None
    status: str


class ProductInternalOut(ProductOut):
    purchase_price_including_gst: Optional[Decimal] = None
    purchase_price_excluding_gst: Optional[Decimal] = None
    purchase_gst_component: Optional[Decimal] = None
    initial_quantity: Optional[Decimal] = None
    source_reference: Optional[str] = None


class ProductImportRequest(BaseModel):
    products: List[ProductCreate] = Field(min_length=1, max_length=500)


class StockAdjustmentCreate(BaseModel):
    movement_type: Literal['PURCHASE', 'ADJUSTMENT_IN', 'ADJUSTMENT_OUT']
    quantity: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    reference_number: Optional[str] = Field(default=None, max_length=120)
    notes: Optional[str] = Field(default=None, max_length=500)
    unit_purchase_price: Optional[Decimal] = Field(default=None, ge=0, max_digits=12, decimal_places=2)
    supplier: Optional[str] = Field(default=None, max_length=160)
    purchase_date: Optional[datetime] = None


class StockMovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    movement_type: str
    quantity_in: Decimal
    quantity_out: Decimal
    previous_stock: Decimal
    new_stock: Decimal
    reference_number: Optional[str] = None
    movement_date: datetime
    notes: Optional[str] = None


class PurchaseEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    product_name: Optional[str] = None
    hsn_code: Optional[str] = None
    master_buying_price_snapshot: Optional[Decimal] = None
    master_selling_price_snapshot: Optional[Decimal] = None
    supplier: Optional[str] = None
    invoice_number: Optional[str] = None
    purchase_date: datetime
    quantity: Decimal
    unit_price_including_gst: Decimal
    gst_rate: Optional[Decimal] = None
    total_amount: Decimal
    notes: Optional[str] = None


# ---------- FARMERS ----------
class FarmerBase(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    mobile: str = Field(min_length=6, max_length=30)
    location: Optional[str] = Field(default=None, max_length=160)
    billing_address: Optional[str] = Field(default=None, max_length=500)
    delivery_address: Optional[str] = Field(default=None, max_length=500)
    gstin: Optional[str] = Field(default=None, max_length=20)
    state_name: Optional[str] = Field(default=None, max_length=80)
    state_code: Optional[str] = Field(default=None, max_length=2)


class FarmerCreate(FarmerBase):
    pass


class FarmerUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=120)
    mobile: Optional[str] = Field(default=None, min_length=6, max_length=30)
    location: Optional[str] = Field(default=None, max_length=160)
    billing_address: Optional[str] = Field(default=None, max_length=500)
    delivery_address: Optional[str] = Field(default=None, max_length=500)
    gstin: Optional[str] = Field(default=None, max_length=20)
    state_name: Optional[str] = Field(default=None, max_length=80)
    state_code: Optional[str] = Field(default=None, max_length=2)


class FarmerOut(FarmerBase):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime


class FarmerProfile(FarmerOut):
    total_purchases: float
    outstanding: float
    bill_count: int


class FarmerListOut(FarmerProfile):
    last_purchase: Optional[datetime] = None


# ---------- BILLING ----------
class BillItemIn(BaseModel):
    product_id: int = Field(gt=0)
    qty: Decimal = Field(gt=0)
    rate: Optional[Decimal] = Field(default=None, ge=0)
    discount_amount: Decimal = Field(default=Decimal('0'), ge=0)
    discount_percentage: Decimal = Field(default=Decimal('0'), ge=0, le=100)
    selling_price_tax_mode: Optional[Literal['inclusive', 'exclusive']] = None


class InvoiceCreate(BaseModel):
    farmer_id: Optional[int] = None
    customer_name: Optional[str] = Field(default=None, max_length=160)
    customer_address: Optional[str] = Field(default=None, max_length=500)
    customer_gstin: Optional[str] = Field(default=None, max_length=20)
    customer_phone: Optional[str] = Field(default=None, max_length=30)
    customer_state_name: Optional[str] = Field(default=None, max_length=80)
    customer_state_code: Optional[str] = Field(default=None, max_length=2)
    place_of_supply_state_name: Optional[str] = Field(default=None, max_length=80)
    place_of_supply_state_code: Optional[str] = Field(default=None, max_length=2)
    items: List[BillItemIn]
    discount: Decimal = Field(default=Decimal('0'), ge=0)
    payment_method: PaymentMethodEnum = PaymentMethodEnum.cash
    amount_paid: Decimal = Field(default=Decimal('0'), ge=0)
    remarks: Optional[str] = Field(default=None, max_length=1000)
    payment_terms: Optional[str] = Field(default=None, max_length=300)
    due_date: Optional[datetime] = None
    invoice_type: Literal['TAX INVOICE', 'CREDIT TAX INVOICE'] = 'TAX INVOICE'


class PaymentCreate(BaseModel):
    amount: float = Field(gt=0)
    method: PaymentMethodEnum


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    amount: float
    method: PaymentMethodEnum
    received_at: datetime


class InvoiceItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_name: str
    unit: str
    qty: float
    rate: float
    amount: float
    gst_rate: Optional[Decimal] = None
    gst_selection_type: Optional[Literal['preset', 'custom']] = None
    selling_price_tax_mode: Optional[Literal['inclusive', 'exclusive']] = None
    taxable_amount: Optional[float] = None
    tax_amount: Optional[float] = None
    total_amount: Optional[float] = None
    hsn_code: Optional[str] = None
    discount_amount: Optional[float] = None
    cgst_amount: Optional[float] = None
    sgst_amount: Optional[float] = None
    igst_amount: Optional[float] = None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    invoice_no: str
    original_invoice_no: Optional[str] = None
    source_database: Optional[str] = None
    farmer_id: Optional[int]
    date: datetime
    subtotal: float
    discount: float
    tax: float
    grand_total: float
    payment_method: PaymentMethodEnum
    amount_paid: float
    balance_due: float
    status: InvoiceStatusEnum
    customer_name: Optional[str] = None
    customer_address: Optional[str] = None
    customer_gstin: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_state_name: Optional[str] = None
    customer_state_code: Optional[str] = None
    place_of_supply_state_name: Optional[str] = None
    place_of_supply_state_code: Optional[str] = None
    cgst_amount: Optional[float] = None
    sgst_amount: Optional[float] = None
    igst_amount: Optional[float] = None
    rounding_adjustment: Optional[float] = None
    amount_in_words: Optional[str] = None
    remarks: Optional[str] = None
    payment_terms: Optional[str] = None
    due_date: Optional[datetime] = None
    invoice_type: Optional[str] = None
    irn: Optional[str] = None
    acknowledgement_number: Optional[str] = None
    acknowledgement_date: Optional[datetime] = None
    eway_bill_number: Optional[str] = None
    items: List[InvoiceItemOut] = []


class InvoiceListOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    invoice_no: str
    original_invoice_no: Optional[str] = None
    source_database: Optional[str] = None
    farmer_id: Optional[int]
    farmer_name: Optional[str] = None
    date: datetime
    grand_total: float
    amount_paid: float
    balance_due: float
    payment_method: PaymentMethodEnum
    status: InvoiceStatusEnum


class BusinessSettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    business_name: str
    address: str
    gstin: str
    state_name: str
    state_code: str
    phone: Optional[str] = None
    bank_name: Optional[str] = None
    account_holder_name: Optional[str] = None
    account_number: Optional[str] = None
    branch: Optional[str] = None
    ifsc_code: Optional[str] = None
    declaration: Optional[str] = None


class BusinessSettingsUpdate(BaseModel):
    business_name: Optional[str] = Field(default=None, min_length=1, max_length=160)
    address: Optional[str] = Field(default=None, max_length=500)
    gstin: Optional[str] = Field(default=None, max_length=20)
    state_name: Optional[str] = Field(default=None, max_length=80)
    state_code: Optional[str] = Field(default=None, max_length=2)
    phone: Optional[str] = Field(default=None, max_length=30)
    bank_name: Optional[str] = Field(default=None, max_length=160)
    account_holder_name: Optional[str] = Field(default=None, max_length=160)
    account_number: Optional[str] = Field(default=None, max_length=80)
    branch: Optional[str] = Field(default=None, max_length=160)
    ifsc_code: Optional[str] = Field(default=None, max_length=20)
    declaration: Optional[str] = Field(default=None, max_length=500)


class InvoiceElectronicDetailsUpdate(BaseModel):
    """Real IRP/e-way bill identifiers returned by an authorized provider; never generated locally."""
    irn: Optional[str] = Field(default=None, max_length=100)
    acknowledgement_number: Optional[str] = Field(default=None, max_length=100)
    acknowledgement_date: Optional[datetime] = None
    eway_bill_number: Optional[str] = Field(default=None, max_length=100)


# ---------- DASHBOARD / REPORTS ----------
class DashboardSummary(BaseModel):
    todays_sales: float
    bills_today: int
    outstanding_total: float
    low_stock_count: int


class SalesPoint(BaseModel):
    label: str
    total: float


class TopProduct(BaseModel):
    name: str
    total: float


class PaymentSplit(BaseModel):
    method: str
    total: float
    percent: float


class OutstandingSummary(BaseModel):
    total_outstanding: float
    due_today: float
    overdue: float
    customers_with_dues: int


class NotificationOut(BaseModel):
    id: str
    type: str
    severity: str
    title: str
    message: str
    created_at: datetime
    action_view: Optional[str] = None
