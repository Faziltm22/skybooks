from decimal import Decimal
from pathlib import Path
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Sum
from django.utils import timezone

def validate_screenshot(file):
    allowed={'.png','.jpg','.jpeg','.webp'}
    if Path(file.name).suffix.lower() not in allowed: raise ValidationError('Upload a PNG, JPG or WebP image.')
    if file.size > 8 * 1024 * 1024: raise ValidationError('Images must be 8 MB or smaller.')
    header=file.read(12); file.seek(0)
    valid=(header.startswith(b'\x89PNG\r\n\x1a\n') or header.startswith(b'\xff\xd8\xff') or (header[:4]==b'RIFF' and header[8:12]==b'WEBP'))
    if not valid: raise ValidationError('The uploaded file is not a supported image.')

class CostCentre(models.Model):
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    active = models.BooleanField(default=True)
    def __str__(self): return f'{self.code} — {self.name}'

class Account(models.Model):
    TYPES = [('ASSET','Asset'),('LIABILITY','Liability'),('EQUITY','Equity'),('INCOME','Income'),('EXPENSE','Expense')]
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=120)
    account_type = models.CharField(max_length=12, choices=TYPES)
    parent = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='children')
    active = models.BooleanField(default=True)
    def __str__(self): return f'{self.code} — {self.name}'

class FinancialYear(models.Model):
    name = models.CharField(max_length=20, unique=True)
    start_date = models.DateField()
    end_date = models.DateField()
    is_closed = models.BooleanField(default=False)
    def clean(self):
        if self.end_date < self.start_date: raise ValidationError('End date must be after start date.')
    def __str__(self): return self.name

class AccountingPeriod(models.Model):
    financial_year = models.ForeignKey(FinancialYear, on_delete=models.PROTECT, related_name='periods')
    name = models.CharField(max_length=30)
    start_date = models.DateField()
    end_date = models.DateField()
    is_locked = models.BooleanField(default=False)
    class Meta: ordering = ['start_date']; unique_together = [('financial_year','name')]
    def __str__(self): return f'{self.financial_year} / {self.name}'

class Voucher(models.Model):
    TYPES = [('JOURNAL','Journal'),('PAYMENT','Payment'),('RECEIPT','Receipt'),('CONTRA','Contra'),('STOCK','Stock adjustment')]
    STATUSES = [('DRAFT','Draft'),('POSTED','Posted'),('REVERSED','Reversed')]
    number = models.CharField(max_length=30, unique=True, blank=True)
    voucher_type = models.CharField(max_length=12, choices=TYPES, default='JOURNAL')
    date = models.DateField(default=timezone.localdate)
    narration = models.CharField(max_length=250)
    status = models.CharField(max_length=10, choices=STATUSES, default='DRAFT')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    reversed_from = models.ForeignKey('self', null=True, blank=True, on_delete=models.PROTECT, related_name='reversals')
    class Meta: ordering = ['-date','-id']
    @property
    def total_debit(self): return self.lines.aggregate(v=Sum('debit'))['v'] or Decimal('0.00')
    @property
    def total_credit(self): return self.lines.aggregate(v=Sum('credit'))['v'] or Decimal('0.00')
    def __str__(self): return self.number or f'Voucher #{self.pk}'
    def post(self, user=None):
        with transaction.atomic():
            v = Voucher.objects.select_for_update().get(pk=self.pk)
            if v.status != 'DRAFT': raise ValidationError('Only draft vouchers can be posted.')
            lines = list(v.lines.select_related('account'))
            if len(lines) < 2: raise ValidationError('A voucher needs at least two lines.')
            if any(x.debit < 0 or x.credit < 0 or (x.debit > 0) == (x.credit > 0) for x in lines):
                raise ValidationError('Each voucher line must contain one non-negative debit or credit amount.')
            if any(not x.account.active for x in lines): raise ValidationError('Inactive accounts cannot be used for posting.')
            debit = sum((x.debit for x in lines), Decimal('0.00'))
            credit = sum((x.credit for x in lines), Decimal('0.00'))
            if debit <= 0 or debit != credit: raise ValidationError('Voucher debits and credits must be equal and greater than zero.')
            year = FinancialYear.objects.filter(start_date__lte=v.date, end_date__gte=v.date).first()
            if not year or year.is_closed: raise ValidationError('Voucher date is outside an open financial year.')
            period = AccountingPeriod.objects.filter(financial_year=year, start_date__lte=v.date, end_date__gte=v.date).first()
            if period and period.is_locked: raise ValidationError('This accounting period is locked.')
            v.number = v.number or f'{v.voucher_type[:3]}-{v.date:%Y%m%d}-{v.pk:06d}'
            v.status, v.posted_at = 'POSTED', timezone.now()
            if user: v.created_by = v.created_by or user
            v.save(update_fields=['number','status','posted_at','created_by'])
            self.refresh_from_db()
    def reverse(self, user=None):
        if self.status != 'POSTED': raise ValidationError('Only posted vouchers can be reversed.')
        with transaction.atomic():
            original = Voucher.objects.select_for_update().get(pk=self.pk)
            if original.reversals.exists(): raise ValidationError('This voucher has already been reversed.')
            rev = Voucher.objects.create(voucher_type='JOURNAL', date=timezone.localdate(), narration=f'Reversal of {original.number}: {original.narration}', created_by=user, reversed_from=original)
            for line in original.lines.all(): VoucherLine.objects.create(voucher=rev, account=line.account, cost_centre=line.cost_centre, description=f'Reversal: {line.description}', debit=line.credit, credit=line.debit)
            rev.post(user)
            original.status = 'REVERSED'; original.save(update_fields=['status'])
            return rev

class VoucherLine(models.Model):
    voucher = models.ForeignKey(Voucher, on_delete=models.PROTECT, related_name='lines')
    account = models.ForeignKey(Account, on_delete=models.PROTECT)
    cost_centre = models.ForeignKey(CostCentre, null=True, blank=True, on_delete=models.PROTECT)
    description = models.CharField(max_length=180, blank=True)
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    def clean(self):
        if self.debit < 0 or self.credit < 0: raise ValidationError('Amounts cannot be negative.')
        if (self.debit > 0) == (self.credit > 0): raise ValidationError('Enter a debit or a credit on each line, not both.')
    def save(self, *args, **kwargs):
        if self.voucher_id and self.voucher.status != 'DRAFT': raise ValidationError('Posted voucher lines are immutable. Create a reversal or adjustment.')
        self.full_clean()
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        if self.voucher.status != 'DRAFT': raise ValidationError('Posted voucher lines are immutable.')
        return super().delete(*args, **kwargs)

class Product(models.Model):
    sku = models.CharField(max_length=40, unique=True, blank=True)
    name = models.CharField(max_length=120)
    unit = models.CharField(max_length=20, default='unit')
    reorder_level = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    sale_price = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    image = models.FileField(upload_to='product_images/%Y/%m/', blank=True, validators=[validate_screenshot])
    active = models.BooleanField(default=True)
    is_archived = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name='stock_items_added')
    def save(self, *args, **kwargs):
        if not self.sku and not self.pk:
            super().save(*args, **kwargs)
            self.sku = f'STK-{self.pk:06d}'
            return super().save(update_fields=['sku'])
        if not self.sku and self.pk:
            self.sku = f'STK-{self.pk:06d}'
        return super().save(*args, **kwargs)
    def clean(self):
        if self.reorder_level < 0 or self.unit_cost < 0 or self.sale_price < 0: raise ValidationError('Stock thresholds and prices cannot be negative.')
    def stock_on_hand(self, condition=None):
        movements = self.movements.filter(is_void=False)
        if condition: movements = movements.filter(condition=condition)
        return movements.aggregate(q=Sum('quantity'))['q'] or Decimal('0.000')
    def __str__(self): return f'{self.sku} — {self.name}'

class StockMovement(models.Model):
    PRODUCT_CONDITIONS = [('FRESH','Fresh / New'),('USED','Used')]
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='movements')
    condition = models.CharField(max_length=8, choices=PRODUCT_CONDITIONS, default='FRESH')
    date = models.DateField(default=timezone.localdate)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, help_text='Positive for stock in, negative for stock out')
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    reference = models.CharField(max_length=60, blank=True)
    notes = models.CharField(max_length=180, blank=True)
    is_void = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ['-date','-id']
    def clean(self):
        if self.quantity == 0: raise ValidationError('Quantity cannot be zero.')
        if self.unit_cost < 0: raise ValidationError('Unit cost cannot be negative.')
    def save(self, *args, **kwargs):
        self.full_clean()
        if self.quantity < 0:
            available = self.product.stock_on_hand(self.condition)
            if self.pk:
                old = StockMovement.objects.get(pk=self.pk)
                available -= old.quantity
            if available + self.quantity < 0: raise ValidationError('This movement would make stock negative.')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        raise ValidationError('Stock movements are immutable. Record a correcting movement instead.')

class StockPurchase(models.Model):
    PAYMENT_CHOICES = [('CASH','Cash'),('UPI','UPI'),('BANK','Bank transfer'),('CREDIT','Credit')]
    STATUS_CHOICES = [('PENDING','Pending'),('PAID','Paid')]
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='purchases')
    movement = models.OneToOneField(StockMovement, on_delete=models.PROTECT, related_name='purchase_record')
    date = models.DateField(default=timezone.localdate)
    supplier = models.CharField(max_length=120, blank=True)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_cost = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    payment_method = models.CharField(max_length=8, choices=PAYMENT_CHOICES, default='CASH')
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default='PAID')
    paid_on = models.DateField(null=True, blank=True)
    cash_entry = models.OneToOneField('CashEntry', null=True, blank=True, on_delete=models.PROTECT, related_name='stock_purchase')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ['-date','-id']
    def clean(self):
        if self.quantity <= 0 or self.amount <= 0: raise ValidationError('Purchase quantity and amount must be greater than zero.')
        if self.unit_cost < 0: raise ValidationError('Purchase unit cost cannot be negative.')
    @property
    def amount_paid(self):
        if self.cash_entry_id and not self.cash_entry.is_void: return self.cash_entry.amount
        if 'payments' in getattr(self,'_prefetched_objects_cache',{}):
            return sum((payment.amount for payment in self.payments.all() if not payment.cash_entry_id or not payment.cash_entry.is_void),Decimal('0.00'))
        return self.payments.filter(models.Q(cash_entry__isnull=True)|models.Q(cash_entry__is_void=False)).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
    @property
    def amount_due(self): return max(Decimal('0.00'),self.amount-self.amount_paid)
    def __str__(self): return f'{self.product.name} · {self.amount}'

class StockPurchasePayment(models.Model):
    METHODS = [('CASH','Cash'),('UPI','UPI'),('BANK','Bank transfer')]
    purchase = models.ForeignKey(StockPurchase, on_delete=models.PROTECT, related_name='payments')
    date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    payment_method = models.CharField(max_length=8, choices=METHODS)
    transaction_id = models.CharField(max_length=100)
    proof = models.FileField(upload_to='stock_purchase_proofs/%Y/%m/', validators=[validate_screenshot])
    cash_entry = models.OneToOneField('CashEntry', null=True, blank=True, on_delete=models.PROTECT, related_name='stock_purchase_payment')
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ['-date','-id']
    def clean(self):
        if self.amount<=0: raise ValidationError('Payment amount must be greater than zero.')
        if not self.transaction_id.strip(): raise ValidationError('Enter the transaction ID or payment reference.')
    def __str__(self): return f'{self.purchase} payment {self.amount}'

class SupplierReturn(models.Model):
    SETTLEMENTS = [('CASH','Cash refund'),('UPI','UPI refund'),('BANK','Bank refund'),('CREDIT','Supplier credit note')]
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='supplier_returns')
    movement = models.OneToOneField(StockMovement, on_delete=models.PROTECT, related_name='supplier_return')
    purchase = models.ForeignKey(StockPurchase, null=True, blank=True, on_delete=models.SET_NULL, related_name='returns')
    supplier = models.CharField(max_length=120)
    date = models.DateField(default=timezone.localdate)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    settlement = models.CharField(max_length=8, choices=SETTLEMENTS, default='CASH')
    cash_entry = models.OneToOneField('CashEntry', null=True, blank=True, on_delete=models.PROTECT, related_name='supplier_return')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ['-date','-id']
    def clean(self):
        if self.quantity <= 0: raise ValidationError('Return quantity must be greater than zero.')
        if self.amount < 0: raise ValidationError('Refund amount cannot be negative.')
        if not self.supplier.strip(): raise ValidationError('Enter the supplier name.')
    def __str__(self): return f'Return to {self.supplier} · {self.product.name}'

class UserModuleAccess(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='module_access')
    cashbook_view = models.BooleanField(default=False)
    cashbook_add = models.BooleanField(default=False)
    cashbook_edit = models.BooleanField(default=False)
    cashbook_delete = models.BooleanField(default=False)
    inventory_view = models.BooleanField(default=False)
    inventory_add = models.BooleanField(default=False)
    inventory_edit = models.BooleanField(default=False)
    inventory_delete = models.BooleanField(default=False)
    sales_view = models.BooleanField(default=False)
    sales_add = models.BooleanField(default=False)
    sales_edit = models.BooleanField(default=False)
    sales_delete = models.BooleanField(default=False)
    reports_view = models.BooleanField(default=False)
    assets_view = models.BooleanField(default=False)
    assets_add = models.BooleanField(default=False)
    assets_edit = models.BooleanField(default=False)
    assets_delete = models.BooleanField(default=False)
    services_view = models.BooleanField(default=False)
    services_add = models.BooleanField(default=False)
    services_edit = models.BooleanField(default=False)
    services_delete = models.BooleanField(default=False)
    internships_view = models.BooleanField(default=False)
    internships_add = models.BooleanField(default=False)
    internships_edit = models.BooleanField(default=False)
    internships_delete = models.BooleanField(default=False)
    def __str__(self): return f'Access for {self.user.username}'

class CashCategory(models.Model):
    TYPES = [('INCOME','Income'),('EXPENSE','Expense')]
    name = models.CharField(max_length=80)
    entry_type = models.CharField(max_length=8, choices=TYPES)
    active = models.BooleanField(default=True)
    class Meta: ordering = ['entry_type','name']; unique_together = [('name','entry_type')]
    def __str__(self): return f'{self.name} ({self.get_entry_type_display()})'

class CashEntry(models.Model):
    entry_type = models.CharField(max_length=8, choices=CashCategory.TYPES)
    date = models.DateField(default=timezone.localdate)
    category = models.ForeignKey(CashCategory, on_delete=models.PROTECT)
    reason = models.CharField(max_length=200)
    party = models.CharField(max_length=120, blank=True, help_text='Who paid or received the money')
    PAYMENT_METHODS = [('CASH','Cash'),('UPI','UPI'),('BANK','Bank transfer'),('OTHER','Other')]
    payment_method = models.CharField(max_length=8, choices=PAYMENT_METHODS, default='CASH')
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    reference = models.CharField(max_length=60, blank=True)
    is_void = models.BooleanField(default=False)
    source_sale = models.ForeignKey('Sale', null=True, blank=True, on_delete=models.PROTECT, related_name='book_entries')
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ['-date','-id']
    def clean(self):
        if self.amount <= 0: raise ValidationError('Amount must be greater than zero.')
        if self.category_id and self.category.entry_type != self.entry_type: raise ValidationError('Choose a category matching the entry type.')
    def save(self, *args, **kwargs):
        if self.pk and CashEntry.objects.filter(pk=self.pk,is_void=True).exists(): raise ValidationError('Voided book entries are preserved as read-only history.')
        if self.pk and self.source_sale_id: raise ValidationError('Sales-linked book entries are managed through the sales log.')
        return super().save(*args, **kwargs)
    def delete(self, *args, **kwargs):
        if self.is_void: raise ValidationError('Voided book entries are preserved as read-only history.')
        if self.source_sale_id: raise ValidationError('Sales-linked book entries are managed through the sales log.')
        return super().delete(*args, **kwargs)
    def __str__(self): return f'{self.get_entry_type_display()} ₹{self.amount} — {self.reason}'

class ExpenseClaim(models.Model):
    CATEGORIES=[('TRAVEL','Travel'),('FOOD','Food'),('LODGING','Lodging'),('TRANSPORT','Local transport'),('SUPPLIES','Supplies'),('OTHER','Other')]
    STATUSES=[('PENDING','Pending'),('APPROVED','Approved'),('REJECTED','Rejected')]
    date=models.DateField(default=timezone.localdate)
    category=models.CharField(max_length=16,choices=CATEGORIES)
    reason=models.CharField(max_length=200)
    amount=models.DecimalField(max_digits=14,decimal_places=2)
    vendor=models.CharField(max_length=120,blank=True)
    reference=models.CharField(max_length=100,blank=True)
    receipt=models.FileField(upload_to='expense_claim_receipts/%Y/%m/',blank=True,validators=[validate_screenshot])
    status=models.CharField(max_length=10,choices=STATUSES,default='PENDING')
    reviewer_note=models.CharField(max_length=240,blank=True)
    submitted_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.SET_NULL,related_name='expense_claims')
    reviewed_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.SET_NULL,related_name='reviewed_expense_claims')
    reviewed_at=models.DateTimeField(null=True,blank=True)
    cash_entry=models.OneToOneField('CashEntry',null=True,blank=True,on_delete=models.PROTECT,related_name='expense_claim')
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['-date','-id']
    def clean(self):
        if self.amount<=0: raise ValidationError('Claim amount must be greater than zero.')
    def __str__(self): return f'{self.get_category_display()} claim ₹{self.amount}'

class Sale(models.Model):
    PAYMENT_TYPES = [('CASH','Cash'),('UPI','UPI'),('BANK','Bank transfer')]
    number = models.CharField(max_length=30, unique=True, blank=True)
    date = models.DateField(default=timezone.localdate)
    customer_name = models.CharField(max_length=120, blank=True)
    customer_address = models.CharField(max_length=240, blank=True)
    customer_phone = models.CharField(max_length=40, blank=True)
    customer_email = models.EmailField(blank=True)
    payment_type = models.CharField(max_length=8, choices=PAYMENT_TYPES, default='CASH')
    notes = models.CharField(max_length=200, blank=True)
    subtotal = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    is_void = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ['-date','-id']
    def __str__(self): return self.number or f'Sale #{self.pk}'

class SaleLine(models.Model):
    sale = models.ForeignKey(Sale, on_delete=models.PROTECT, related_name='lines')
    product = models.ForeignKey(Product, on_delete=models.PROTECT)
    condition = models.CharField(max_length=8, choices=StockMovement.PRODUCT_CONDITIONS)
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    discount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    @property
    def line_total(self): return self.quantity * self.unit_price
    @property
    def net_total(self): return self.line_total - self.discount

class InternshipRecord(models.Model):
    STATUSES = [('APPLIED','Applied'),('ACTIVE','Active'),('COMPLETED','Completed'),('CANCELLED','Cancelled')]
    number = models.CharField(max_length=30, unique=True, blank=True)
    name = models.CharField(max_length=120)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    program = models.CharField(max_length=120)
    mentor = models.CharField(max_length=120, blank=True)
    start_date = models.DateField(default=timezone.localdate)
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUSES, default='APPLIED')
    fee = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    fee_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ['-start_date','-id']
    def clean(self):
        if self.fee < 0 or self.fee_paid < 0 or self.fee_paid > self.fee: raise ValidationError('Internship fee and paid amount must be valid; paid cannot exceed the fee.')
        if self.end_date and self.end_date < self.start_date: raise ValidationError('End date cannot be before start date.')
    def save(self, *args, **kwargs):
        if not self.number and not self.pk:
            super().save(*args, **kwargs)
            self.number=f'INT-{self.pk:06d}'
            return super().save(update_fields=['number'])
        return super().save(*args, **kwargs)

class InternshipPayment(models.Model):
    internship=models.ForeignKey(InternshipRecord,on_delete=models.PROTECT,related_name='payments')
    date=models.DateField(default=timezone.localdate)
    amount=models.DecimalField(max_digits=12,decimal_places=2)
    payment_method=models.CharField(max_length=8,choices=CashEntry.PAYMENT_METHODS,default='CASH')
    reference=models.CharField(max_length=100,blank=True)
    cash_entry=models.OneToOneField(CashEntry,null=True,blank=True,on_delete=models.PROTECT,related_name='internship_payment')
    is_void = models.BooleanField(default=False)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['-date','-id']
    def clean(self):
        if self.amount<=0: raise ValidationError('Internship payment must be greater than zero.')
    def __str__(self): return f'{self.internship.number} payment ₹{self.amount}'

class ActivityLog(models.Model):
    ACTIONS=[('CREATE','Created'),('UPDATE','Updated'),('DELETE','Deleted'),('VOID','Voided'),('ARCHIVE','Archived')]
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    entity_type = models.CharField(max_length=32)
    object_id = models.CharField(max_length=40)
    object_label = models.CharField(max_length=160)
    action = models.CharField(max_length=8, choices=ACTIONS)
    details = models.CharField(max_length=240, blank=True)
    before_data = models.JSONField(null=True, blank=True)
    after_data = models.JSONField(null=True, blank=True)
    recovered_from = models.ForeignKey('self',null=True,blank=True,on_delete=models.SET_NULL,related_name='recoveries')
    created_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ['-created_at','-id']

class BusinessAsset(models.Model):
    TYPES=[('DRONE','Drone'),('CAMERA','Camera'),('COMPUTER','Computer / laptop'),('PHONE','Phone / tablet'),('SOFTWARE','Software / licence'),('EQUIPMENT','Other equipment'),('VEHICLE','Vehicle'),('OTHER','Other asset')]
    STATUSES=[('ACTIVE','In use'),('REPAIR','In repair'),('RETIRED','Retired'),('LOST','Lost')]
    CONDITIONS=[('FRESH','New'),('USED','Used'),('REPAIR','Needs repair')]
    asset_tag=models.CharField(max_length=40,unique=True)
    name=models.CharField(max_length=140)
    asset_type=models.CharField(max_length=12,choices=TYPES)
    brand_model=models.CharField(max_length=120,blank=True)
    serial_number=models.CharField(max_length=100,blank=True)
    condition=models.CharField(max_length=8,choices=CONDITIONS,default='FRESH')
    status=models.CharField(max_length=8,choices=STATUSES,default='ACTIVE')
    purchase_date=models.DateField(null=True,blank=True)
    purchase_cost=models.DecimalField(max_digits=14,decimal_places=2,default=0)
    assigned_to=models.CharField(max_length=120,blank=True)
    location=models.CharField(max_length=120,blank=True)
    license_expiry=models.DateField(null=True,blank=True)
    screenshot=models.FileField(upload_to='asset_documents/%Y/%m/',blank=True,validators=[validate_screenshot])
    notes=models.TextField(blank=True)
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.SET_NULL)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['asset_type','name']
    def clean(self):
        if self.purchase_cost<0: raise ValidationError('Purchase cost cannot be negative.')
    def __str__(self): return f'{self.asset_tag} · {self.name}'

class BusinessAssetImage(models.Model):
    asset = models.ForeignKey(BusinessAsset, on_delete=models.CASCADE, related_name='images')
    image = models.FileField(upload_to='asset_documents/%Y/%m/', validators=[validate_screenshot])
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    class Meta: ordering = ['uploaded_at','id']
    def clean(self):
        if self.asset_id:
            image_count=self.asset.images.exclude(pk=self.pk).count()+int(bool(self.asset.screenshot))
            if image_count>=5: raise ValidationError({'image':'An asset can have at most 5 images, including its existing receipt image.'})
    def __str__(self): return f'Image for {self.asset.asset_tag}'

class ServiceRecord(models.Model):
    TYPES=[('DRONE','Drone service'),('REPAIR','Repair'),('TRAINING','Training'),('INSTALL','Installation'),('CONSULT','Consulting'),('OTHER','Other')]
    STATUSES=[('PENDING','Pending'),('IN_PROGRESS','In progress'),('COMPLETED','Completed')]
    number=models.CharField(max_length=30,unique=True,blank=True,null=True,default=None)
    title=models.CharField(max_length=160)
    service_type=models.CharField(max_length=10,choices=TYPES,default='DRONE')
    customer_name=models.CharField(max_length=140,blank=True)
    contact=models.CharField(max_length=80,blank=True)
    description=models.TextField(blank=True)
    status=models.CharField(max_length=12,choices=STATUSES,default='PENDING')
    opened_date=models.DateField(default=timezone.localdate)
    due_date=models.DateField(null=True,blank=True)
    completed_date=models.DateField(null=True,blank=True)
    charge=models.DecimalField(max_digits=14,decimal_places=2,default=0)
    screenshot=models.FileField(upload_to='service_screenshots/%Y/%m/',blank=True,validators=[validate_screenshot])
    created_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.SET_NULL)
    created_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['-opened_date','-id']
    def clean(self):
        if self.charge<0: raise ValidationError('Service charge cannot be negative.')
        if self.due_date and self.due_date<self.opened_date: raise ValidationError('Due date cannot be before the opened date.')
        if self.status=='COMPLETED' and not self.completed_date: self.completed_date=timezone.localdate()
        if self.status!='COMPLETED': self.completed_date=None
    def save(self,*args,**kwargs):
        if not self.number and not self.pk:
            super().save(*args,**kwargs)
            self.number=f'SRV-{self.opened_date:%Y%m%d}-{self.pk:06d}'
            return super().save(update_fields=['number'])
        if not self.number and self.pk: self.number=f'SRV-{self.opened_date:%Y%m%d}-{self.pk:06d}'
        return super().save(*args,**kwargs)
    def __str__(self): return self.number or self.title

class ServiceRequest(models.Model):
    STATUSES=[('WAITING','Waiting for review'),('ACCEPTED','Accepted'),('REJECTED','Rejected')]
    title=models.CharField(max_length=160)
    service_type=models.CharField(max_length=10,choices=ServiceRecord.TYPES,default='DRONE')
    customer_name=models.CharField(max_length=140)
    email=models.EmailField(blank=True)
    contact=models.CharField(max_length=80)
    description=models.TextField(blank=True)
    status=models.CharField(max_length=10,choices=STATUSES,default='WAITING')
    admin_note=models.CharField(max_length=240,blank=True)
    service=models.OneToOneField(ServiceRecord,null=True,blank=True,on_delete=models.SET_NULL,related_name='client_request')
    submitted_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.SET_NULL,related_name='service_requests')
    reviewed_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,blank=True,on_delete=models.SET_NULL,related_name='reviewed_service_requests')
    requested_at=models.DateTimeField(auto_now_add=True)
    reviewed_at=models.DateTimeField(null=True,blank=True)
    class Meta: ordering=['-requested_at','-id']
    def clean(self):
        if not self.title.strip() or not self.customer_name.strip() or not self.contact.strip(): raise ValidationError('Enter a service title, your name, and a contact number.')
    def __str__(self): return f'{self.title} request from {self.customer_name}'

class ServiceImage(models.Model):
    service=models.ForeignKey(ServiceRecord,on_delete=models.CASCADE,related_name='images')
    image=models.FileField(upload_to='service_screenshots/%Y/%m/',validators=[validate_screenshot])
    uploaded_by=models.ForeignKey(settings.AUTH_USER_MODEL,null=True,on_delete=models.SET_NULL)
    uploaded_at=models.DateTimeField(auto_now_add=True)
    class Meta: ordering=['uploaded_at','id']
    def __str__(self): return f'Image for {self.service.number}'

