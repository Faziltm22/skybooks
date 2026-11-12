from datetime import date, timedelta
import calendar
from decimal import Decimal, InvalidOperation
from functools import wraps
from urllib.parse import urlencode
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.apps import apps
from django.contrib.auth.decorators import login_required
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.db.models import Q, Sum
from django.db.models.functions import TruncMonth
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from .models import Account, ActivityLog, BusinessAsset, BusinessAssetImage, CashCategory, CashEntry, CostCentre, ExpenseClaim, FinancialYear, InternshipPayment, InternshipRecord, Product, Sale, SaleLine, ServiceImage, ServiceRecord, StockMovement, StockPurchase, StockPurchasePayment, SupplierReturn, UserModuleAccess, Voucher, VoucherLine

def _audit_snapshot(obj):
    if obj is None: return None
    data={'_model':obj._meta.label_lower}
    for field in obj._meta.concrete_fields:
        if field.primary_key or field.name == 'password': continue
        value=getattr(obj,field.attname)
        if value is None or isinstance(value,(str,int,float,bool)): data[field.attname]=value
        else: data[field.attname]=value.isoformat() if hasattr(value,'isoformat') else str(value)
    return data

def record_audit(user, entity, obj, action, details='', before_data=None):
    model_label=obj._meta.label_lower
    try: current=obj.__class__._default_manager.get(pk=obj.pk)
    except obj.__class__.DoesNotExist: current=obj
    latest=ActivityLog.objects.filter(entity_type=entity,object_id=str(obj.pk)).first()
    current_data=_audit_snapshot(current)
    if action=='CREATE': before_data=None; after_data=current_data
    elif action=='DELETE': before_data=(latest.after_data if latest and latest.after_data else current_data); after_data=None
    else:
        before_data=before_data or (latest.after_data if latest and latest.after_data else current_data)
        after_data=current_data
    if before_data is not None: before_data.setdefault('_model',model_label)
    ActivityLog.objects.create(actor=user,entity_type=entity,object_id=str(obj.pk),object_label=str(obj),action=action,details=details[:240],before_data=before_data,after_data=after_data)

def record_stock_purchase(movement, amount, method, supplier, user):
    if method not in ('CASH','UPI','BANK','CREDIT'): raise ValidationError('Choose cash, UPI, bank transfer, or credit for this purchase.')
    purchase=StockPurchase(product=movement.product,movement=movement,date=movement.date,supplier=supplier,quantity=movement.quantity,unit_cost=movement.unit_cost,amount=amount,payment_method=method,status='PENDING' if method=='CREDIT' else 'PAID',paid_on=None if method=='CREDIT' else movement.date,created_by=user)
    purchase.full_clean(); purchase.save()
    if method!='CREDIT':
        category,_=CashCategory.objects.get_or_create(name='Stock purchases',entry_type='EXPENSE')
        entry=CashEntry.objects.create(entry_type='EXPENSE',date=movement.date,category=category,reason=f'Stock purchase · {movement.product.name}',party=supplier,payment_method=method,amount=amount,reference=f'PURCHASE-{purchase.pk:06d}',created_by=user)
        purchase.cash_entry=entry; purchase.save(update_fields=['cash_entry'])
    record_audit(user,'STOCK_PURCHASE',purchase,'CREATE','Stock purchase recorded as '+('credit pending payment' if method=='CREDIT' else method.lower()+' expense'))
    return purchase

def module_required(module):
    def decorate(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            if request.user.is_superuser: return view(request, *args, **kwargs)
            access = UserModuleAccess.objects.filter(user=request.user).first()
            if '_' in module:
                parent_view=f'{module.split("_",1)[0]}_view'
                if not access or not getattr(access,parent_view,False): return render(request, 'core/forbidden.html', status=403)
            if not access or not getattr(access, module, False): return render(request, 'core/forbidden.html', status=403)
            return view(request, *args, **kwargs)
        return wrapped
    return decorate

def superadmin_required(view):
    @wraps(view)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser: return render(request, 'core/forbidden.html', status=403)
        return view(request, *args, **kwargs)
    return wrapped

@login_required
def dashboard(request):
    today = timezone.localdate()
    fy = FinancialYear.objects.filter(start_date__lte=today, end_date__gte=today).first()
    entries=CashEntry.objects.filter(is_void=False)
    if fy: entries=entries.filter(date__gte=fy.start_date,date__lte=fy.end_date)
    income=sum((e.amount for e in entries.filter(entry_type='INCOME')),Decimal('0.00'))
    expenses=sum((e.amount for e in entries.filter(entry_type='EXPENSE')),Decimal('0.00'))
    access=UserModuleAccess.objects.filter(user=request.user).first()
    can_book=request.user.is_superuser or bool(access and (access.cashbook_view or access.reports_view))
    can_inventory=request.user.is_superuser or bool(access and access.inventory_view)
    can_services=request.user.is_superuser or bool(access and access.services_view)
    can_assets=request.user.is_superuser or bool(access and access.assets_view)
    category_rows=list(entries.values('entry_type','category__name').annotate(total=Sum('amount')).order_by('-total')) if can_book else []
    income_rows=[row for row in category_rows if row['entry_type']=='INCOME']
    expense_rows=[row for row in category_rows if row['entry_type']=='EXPENSE']
    income_max=max((row['total'] for row in income_rows),default=Decimal('0.00'))
    expense_max=max((row['total'] for row in expense_rows),default=Decimal('0.00'))
    income_categories=[{'name':row['category__name'] or 'Uncategorized','total':row['total'],'percent':float(row['total']/income_max*100) if income_max else 0} for row in income_rows[:5]]
    expense_categories=[{'name':row['category__name'] or 'Uncategorized','total':row['total'],'percent':float(row['total']/expense_max*100) if expense_max else 0} for row in expense_rows[:5]]
    pending_purchases=StockPurchase.objects.filter(status='PENDING').prefetch_related('payments') if can_inventory else StockPurchase.objects.none()
    pending_purchase_total=sum((purchase.amount_due for purchase in pending_purchases),Decimal('0.00'))
    low_stock=[p for p in Product.objects.filter(active=True,is_archived=False) if p.stock_on_hand('FRESH') + p.stock_on_hand('USED') <= p.reorder_level][:8] if can_inventory else []
    monthly=[]
    for offset in range(5,-1,-1):
        index=today.year*12+today.month-1-offset; year=index//12; month=index%12+1
        first=date(year,month,1); last=date(year,month,calendar.monthrange(year,month)[1])
        month_entries=CashEntry.objects.filter(is_void=False,date__gte=first,date__lte=last)
        monthly.append({'month':calendar.month_abbr[month],'income':float(month_entries.filter(entry_type='INCOME').aggregate(total=Sum('amount'))['total'] or 0),'expenses':float(month_entries.filter(entry_type='EXPENSE').aggregate(total=Sum('amount'))['total'] or 0)})
    return render(request, 'core/dashboard.html', {'fy':fy,'income':income if can_book else None,'expenses':expenses if can_book else None,'profit':income-expenses if can_book else None,'has_financial_access':can_book,'income_categories':income_categories,'expense_categories':expense_categories,'recent_entries':entries.select_related('category')[:8] if can_book else [],'low_stock':low_stock,'access':access,'chart_data':monthly,'pending_purchase_total':pending_purchase_total,'pending_purchase_count':pending_purchases.count() if can_inventory else 0,'service_count':ServiceRecord.objects.exclude(status='COMPLETED').count() if can_services else 0,'asset_count':BusinessAsset.objects.exclude(status='RETIRED').count() if can_assets else 0})

@login_required
def global_search(request):
    query=request.GET.get('q','').strip()[:100]
    access=UserModuleAccess.objects.filter(user=request.user).first()
    allowed=lambda module: request.user.is_superuser or bool(access and getattr(access,f'{module}_view',False))
    groups=[]
    if query:
        if allowed('cashbook') or allowed('reports'):
            entries=CashEntry.objects.filter(is_void=False).filter(Q(reason__icontains=query)|Q(reference__icontains=query)|Q(party__icontains=query)|Q(category__name__icontains=query)).select_related('category')[:25]
            if entries: groups.append({'name':'Cashbook','items':[{'title':f'{e.get_entry_type_display()} · ₹{e.amount:.2f}','detail':f'{e.date} · {e.category.name} · {e.reason}','url':('/cashbook/' if allowed('cashbook') else '/reports/')+'?'+urlencode({'q':query})} for e in entries]})
        if allowed('inventory'):
            products=Product.objects.filter(active=True,is_archived=False).filter(Q(name__icontains=query)|Q(sku__icontains=query)|Q(unit__icontains=query))[:25]
            if products: groups.append({'name':'Stock items','items':[{'title':f'{p.sku} · {p.name}','detail':f'Fresh {p.stock_on_hand("FRESH")} · Used {p.stock_on_hand("USED")} {p.unit}','url':'/inventory/?'+urlencode({'condition':'FRESH','q':query})} for p in products]})
            movements=StockMovement.objects.filter(Q(reference__icontains=query)|Q(notes__icontains=query)|Q(product__name__icontains=query),is_void=False).select_related('product')[:25]
            if movements: groups.append({'name':'Stock activity','items':[{'title':f'{m.product.name} · {m.get_condition_display()} · {m.quantity}','detail':f'{m.date} · {m.reference} · {m.notes}','url':'/inventory/?'+urlencode({'condition':m.condition,'q':query})} for m in movements]})
        if allowed('sales'):
            sales=Sale.objects.filter(Q(number__icontains=query)|Q(customer_name__icontains=query)|Q(notes__icontains=query)|Q(lines__product__name__icontains=query)|Q(lines__product__sku__icontains=query)).distinct().prefetch_related('lines__product')[:25]
            if sales: groups.append({'name':'Sales','items':[{'title':f'{s.number} · ₹{s.total:.2f}','detail':f'{s.date} · {s.customer_name or "Walk-in"} · {"Voided" if s.is_void else s.get_payment_type_display()}','url':'/sales/?'+urlencode({'q':query})} for s in sales]})
        if allowed('assets'):
            assets=BusinessAsset.objects.filter(Q(asset_tag__icontains=query)|Q(name__icontains=query)|Q(brand_model__icontains=query)|Q(serial_number__icontains=query)|Q(assigned_to__icontains=query)|Q(location__icontains=query))[:25]
            if assets: groups.append({'name':'Assets & licences','items':[{'title':f'{a.asset_tag} · {a.name}','detail':f'{a.get_asset_type_display()} · {a.get_status_display()} · {a.serial_number}','url':'/assets/?'+urlencode({'q':query})} for a in assets]})
        if allowed('services'):
            records=ServiceRecord.objects.filter(Q(number__icontains=query)|Q(title__icontains=query)|Q(customer_name__icontains=query)|Q(contact__icontains=query)|Q(description__icontains=query))[:25]
            if records: groups.append({'name':'Services','items':[{'title':f'{s.number} · {s.title}','detail':f'{s.get_status_display()} · {s.customer_name or "Internal service"} · {s.opened_date}','url':'/services/?'+urlencode({'q':query})} for s in records]})
        if request.user.is_superuser:
            users=get_user_model().objects.filter(Q(username__icontains=query)|Q(first_name__icontains=query)|Q(last_name__icontains=query)|Q(email__icontains=query))[:25]
            if users: groups.append({'name':'Users','items':[{'title':u.get_full_name() or u.username,'detail':f'{u.username} · {"Active" if u.is_active else "Inactive"} · {"Super admin" if u.is_superuser else "Team member"}','url':'/manage/users/?'+urlencode({'q':query})} for u in users]})
            categories=CashCategory.objects.filter(name__icontains=query)[:25]
            if categories: groups.append({'name':'Cashbook categories','items':[{'title':c.name,'detail':f'{c.get_entry_type_display()} · {"Active" if c.active else "Inactive"}','url':'/manage/categories/?'+urlencode({'q':query})} for c in categories]})
        if allowed('reports'):
            vouchers=Voucher.objects.filter(Q(number__icontains=query)|Q(narration__icontains=query)).select_related('created_by')[:25]
            if vouchers: groups.append({'name':'Vouchers','items':[{'title':f'{v.number or "Draft"} · {v.get_status_display()}','detail':f'{v.date} · {v.narration}','url':'/vouchers/?'+urlencode({'q':query})} for v in vouchers]})
    return render(request,'core/search.html',{'query':query,'groups':groups,'has_searched':bool(query)})

@module_required('reports_view')
def voucher_list(request):
    query=request.GET.get('q','').strip()
    vouchers=Voucher.objects.filter(Q(number__icontains=query)|Q(narration__icontains=query)) if query else Voucher.objects.all()
    return render(request,'core/vouchers.html',{'vouchers':vouchers.prefetch_related('lines__account')[:100],'query':query})

@module_required('reports_view')
@transaction.atomic
def voucher_create(request):
    accounts = Account.objects.filter(active=True).order_by('code')
    centres = CostCentre.objects.filter(active=True)
    if request.method == 'POST':
        try:
            d = date.fromisoformat(request.POST.get('date',''))
            v = Voucher.objects.create(voucher_type=request.POST.get('voucher_type','JOURNAL'), date=d, narration=request.POST.get('narration','').strip(), created_by=request.user)
            if not v.narration: raise ValidationError('Narration is required.')
            for i in range(1, 9):
                acc = request.POST.get(f'account_{i}')
                debit = Decimal(request.POST.get(f'debit_{i}') or '0')
                credit = Decimal(request.POST.get(f'credit_{i}') or '0')
                if acc and (debit or credit):
                    VoucherLine.objects.create(voucher=v, account_id=acc, cost_centre_id=request.POST.get(f'centre_{i}') or None, description=request.POST.get(f'description_{i}',''), debit=debit, credit=credit)
            if request.POST.get('action') == 'post': v.post(request.user); messages.success(request,'Voucher posted.')
            else: messages.success(request,'Draft voucher saved.')
            return redirect('vouchers')
        except (ValueError, InvalidOperation, ValidationError) as e:
            messages.error(request, '; '.join(e.messages) if hasattr(e,'messages') else str(e))
    return render(request,'core/voucher_form.html',{'accounts':accounts,'centres':centres,'today':timezone.localdate(),'types':Voucher.TYPES})

@module_required('reports_view')
def voucher_detail(request, pk):
    v=get_object_or_404(Voucher.objects.prefetch_related('lines__account'),pk=pk)
    return render(request,'core/voucher_detail.html',{'v':v})

@module_required('reports_view')
def voucher_post(request, pk):
    if request.method == 'POST':
        v=get_object_or_404(Voucher,pk=pk)
        try: v.post(request.user); messages.success(request,'Voucher posted successfully.')
        except ValidationError as e: messages.error(request,'; '.join(e.messages))
    return redirect('voucher_detail',pk=pk)

@module_required('reports_view')
def voucher_reverse(request, pk):
    if request.method == 'POST':
        v=get_object_or_404(Voucher,pk=pk)
        try:
            r=v.reverse(request.user); messages.success(request,f'Reversal posted as {r.number}.')
        except ValidationError as e: messages.error(request,'; '.join(e.messages))
    return redirect('voucher_detail',pk=pk)

@module_required('reports_view')
def reports(request):
    start=request.GET.get('start',''); end=request.GET.get('end',''); query=request.GET.get('q','').strip()
    kind=request.GET.get('type',''); condition=request.GET.get('condition','FRESH')
    report_type=request.GET.get('report','')
    report_types=('financial','categories','stock','movements','transactions')
    if report_type not in report_types: report_type=''
    if kind not in ('INCOME','EXPENSE'): kind=''
    if condition not in ('FRESH','USED'): condition='FRESH'
    qs=CashEntry.objects.filter(is_void=False).select_related('category')
    movements=StockMovement.objects.filter(is_void=False).select_related('product','created_by')
    if query:
        qs=qs.filter(Q(reason__icontains=query)|Q(reference__icontains=query)|Q(category__name__icontains=query)|Q(party__icontains=query))
        movements=movements.filter(Q(product__name__icontains=query)|Q(product__sku__icontains=query)|Q(reference__icontains=query)|Q(notes__icontains=query))
    if kind: qs=qs.filter(entry_type=kind)
    try:
        if start:
            date.fromisoformat(start); qs=qs.filter(date__gte=start); movements=movements.filter(date__gte=start)
        if end:
            date.fromisoformat(end); qs=qs.filter(date__lte=end); movements=movements.filter(date__lte=end)
    except ValueError:
        messages.error(request,'Use valid dates in the report filters.')
    income=sum((e.amount for e in qs.filter(entry_type='INCOME')),Decimal('0.00'))
    expenses=sum((e.amount for e in qs.filter(entry_type='EXPENSE')),Decimal('0.00'))
    category_totals=[]
    for category in qs.values('category__name','entry_type').annotate(total=Sum('amount')).order_by('entry_type','category__name'):
        category_totals.append(category)
    expense_totals=list(qs.filter(entry_type='EXPENSE').values('category__name').annotate(total=Sum('amount')).order_by('-total'))
    monthly_totals=qs.annotate(month=TruncMonth('date')).values('month','entry_type').annotate(total=Sum('amount')).order_by('month')
    month_map={}
    for row in monthly_totals:
        key=row['month'].strftime('%Y-%m')
        month_map.setdefault(key,{'month':key,'income':0,'expenses':0})
        month_map[key]['income' if row['entry_type']=='INCOME' else 'expenses']=float(row['total'])
    expense_chart=list(month_map.values())
    stock_rows=[]
    for product in Product.objects.filter(active=True,is_archived=False).order_by('sku'):
        fresh=product.stock_on_hand('FRESH'); used=product.stock_on_hand('USED')
        if (fresh or used) and (not query or query.lower() in f'{product.sku} {product.name}'.lower()):
            stock_rows.append({'product':product,'fresh':fresh,'used':used,'total':fresh+used,'value':(fresh+used)*product.unit_cost})
    all_entries=qs.order_by('-date','-id')
    all_movement_rows=movements.filter(condition=condition).order_by('-date','-id')
    entries=list(all_entries[:500])
    movement_rows=list(all_movement_rows[:200])
    if request.GET.get('export')=='excel' and report_type:
        from .exports import make_xlsx
        transaction_rows=[['Date','Type','Category','Reason','Party','Reference','Amount']]
        transaction_rows.extend([[e.date,e.get_entry_type_display(),e.category.name if e.category else '',e.reason,e.party,e.reference,e.amount] for e in all_entries])
        category_rows=[['Type','Category','Total']]+[[r['entry_type'],r['category__name'] or 'Uncategorized',r['total']] for r in category_totals]
        stock_export=[['Stock ID','Item','Fresh','Used','Total','Unit','Estimated cost value']]+[[r['product'].sku,r['product'].name,r['fresh'],r['used'],r['total'],r['product'].unit,r['value']] for r in stock_rows]
        movement_export=[['Date','Stock ID','Item','Condition','Quantity','Unit','Reference','Notes','Recorded by']]+[[m.date,m.product.sku,m.product.name,m.get_condition_display(),m.quantity,m.product.unit,m.reference,m.notes,str(m.created_by or '')] for m in all_movement_rows]
        if report_type=='financial':
            sheets=[('Summary',[['Metric','Amount'],['Income',income],['Expenses',expenses],['Net profit / loss',income-expenses]]),('By category',category_rows),('Transactions',transaction_rows)]
        elif report_type=='categories': sheets=[('Category totals',category_rows)]
        elif report_type=='stock': sheets=[('Current stock',stock_export)]
        elif report_type=='movements': sheets=[('Stock movements',movement_export)]
        else: sheets=[('Transactions',transaction_rows)]
        response=HttpResponse(make_xlsx(sheets),content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition']=f'attachment; filename="starnux_{report_type}_report.xlsx"'
        return response
    return render(request,'core/reports.html',{'entries':entries,'start':start,'end':end,'query':query,'kind':kind,'condition':condition,'income':income,'expenses':expenses,'net':income-expenses,'category_totals':category_totals,'expense_totals':expense_totals,'expense_chart':expense_chart,'stock_rows':stock_rows,'movements':movement_rows,'report_type':report_type,'report_types':(('financial','Financial summary'),('categories','Totals by category'),('stock','Current stock'),('movements','Stock movements'),('transactions','Transactions'))})

@module_required('inventory_view')
def inventory(request):
    status=request.GET.get('status','ACTIVE')
    if status not in ('ACTIVE','INACTIVE','ARCHIVED') or (status!='ACTIVE' and not request.user.is_superuser):
        status='ACTIVE'
    if status=='ARCHIVED':
        products=Product.objects.filter(is_archived=True)
    elif status=='INACTIVE':
        products=Product.objects.filter(active=False,is_archived=False)
    else:
        products=Product.objects.filter(active=True,is_archived=False)
    products=products.order_by('sku')
    condition=request.GET.get('condition','FRESH')
    if condition not in ('FRESH','USED'): condition='FRESH'
    query=request.GET.get('q','').strip()
    if query: products=products.filter(Q(name__icontains=query)|Q(sku__icontains=query)|Q(unit__icontains=query))
    rows=[{'product':p,'quantity':qty} for p in products if (qty := p.stock_on_hand(condition)) > 0 or status!='ACTIVE']
    movement_qs=StockMovement.objects.filter(condition=condition).select_related('product','created_by')
    if query: movement_qs=movement_qs.filter(Q(reference__icontains=query)|Q(notes__icontains=query)|Q(product__name__icontains=query)|Q(product__sku__icontains=query))
    movement_rows=list(movement_qs)
    for movement in movement_rows: movement.can_change=not movement.reference.startswith(('SALE-','VOID-'))
    returns=SupplierReturn.objects.select_related('product','created_by','cash_entry').all()[:100]
    return render(request,'core/inventory.html',{'rows':rows,'condition':condition,'query':query,'status':status,'movements':movement_rows,'returns':returns,'audit_logs':ActivityLog.objects.filter(entity_type__in=('STOCK_ITEM','STOCK_MOVEMENT','SUPPLIER_RETURN')).select_related('actor')})

@module_required('inventory_add')
@transaction.atomic
def supplier_return_create(request):
    if request.method=='POST':
        try:
            product=get_object_or_404(Product,pk=request.POST.get('product'),active=True,is_archived=False)
            condition=request.POST.get('condition','FRESH')
            if condition not in ('FRESH','USED'): raise ValidationError('Choose a valid stock condition.')
            quantity=Decimal(request.POST.get('quantity') or '0')
            amount=Decimal(request.POST.get('amount') or '0')
            supplier=request.POST.get('supplier','').strip()
            settlement=request.POST.get('settlement','CASH')
            if settlement not in dict(SupplierReturn.SETTLEMENTS): raise ValidationError('Choose a valid refund type.')
            return_date=date.fromisoformat(request.POST.get('date') or timezone.localdate().isoformat())
            movement=StockMovement.objects.create(product=product,condition=condition,date=return_date,quantity=-quantity,unit_cost=product.unit_cost,reference='SUPPLIER-RETURN',notes=f'Returned to {supplier}',created_by=request.user)
            returned=SupplierReturn(product=product,movement=movement,supplier=supplier,date=return_date,quantity=quantity,amount=amount,settlement=settlement,created_by=request.user)
            returned.full_clean(); returned.save()
            if amount>0 and settlement!='CREDIT':
                category,_=CashCategory.objects.get_or_create(name='Supplier refunds',entry_type='INCOME')
                entry=CashEntry.objects.create(entry_type='INCOME',date=return_date,category=category,reason=f'Supplier refund · {product.name}',party=supplier,payment_method=settlement,amount=amount,reference=f'SUPPLIER-RETURN-{returned.pk:06d}',created_by=request.user)
                returned.cash_entry=entry; returned.save(update_fields=['cash_entry'])
            record_audit(request.user,'SUPPLIER_RETURN',returned,'CREATE',f'{quantity} {product.unit} returned; '+('supplier credit note' if settlement=='CREDIT' else f'{settlement.lower()} refund ₹{amount:.2f}'))
            record_audit(request.user,'STOCK_MOVEMENT',movement,'CREATE','Stock returned to supplier')
            messages.success(request,'Supplier return saved. Stock and refund records were updated.')
            return redirect('inventory')
        except (KeyError,ValueError,InvalidOperation,ValidationError) as exc:
            transaction.set_rollback(True)
            messages.error(request,'; '.join(exc.messages) if hasattr(exc,'messages') else f'Please check return details: {exc}')
    return render(request,'core/supplier_return_form.html',{'products':Product.objects.filter(active=True,is_archived=False).order_by('name'),'today':timezone.localdate(),'settlements':SupplierReturn.SETTLEMENTS,'conditions':StockMovement.PRODUCT_CONDITIONS})

@superadmin_required
def activity_history(request):
    logs=ActivityLog.objects.select_related('actor','recovered_from').all()
    action=request.GET.get('action','')
    query=request.GET.get('q','').strip()
    if action in dict(ActivityLog.ACTIONS): logs=logs.filter(action=action)
    if query: logs=logs.filter(Q(object_label__icontains=query)|Q(entity_type__icontains=query)|Q(details__icontains=query)|Q(actor__username__icontains=query))
    return render(request,'core/activity_history.html',{'logs':logs[:300],'action':action,'query':query,'actions':ActivityLog.ACTIONS})

@superadmin_required
@transaction.atomic
def activity_recover(request,pk):
    if request.method!='POST': return redirect('activity_history')
    log=get_object_or_404(ActivityLog.objects.select_for_update(),pk=pk)
    newest=ActivityLog.objects.filter(entity_type=log.entity_type,object_id=log.object_id).first()
    if newest.pk!=log.pk or not log.before_data and log.action!='CREATE':
        messages.error(request,'Only the latest action with a saved previous state can be recovered.')
        return redirect('activity_history')
    model_label=(log.after_data or log.before_data or {}).get('_model')
    if not model_label or not (model_label.startswith('core.') or model_label==get_user_model()._meta.label_lower):
        messages.error(request,'This record type cannot be recovered from the activity log.')
        return redirect('activity_history')
    model=apps.get_model(model_label)
    if model is ActivityLog:
        messages.error(request,'Activity records cannot be recovered or removed here.')
        return redirect('activity_history')
    current=model._default_manager.filter(pk=log.object_id).first()
    previous_state=_audit_snapshot(current)
    try:
        if log.action=='CREATE':
            if current is None: raise ValidationError('The created record is already missing.')
            if isinstance(current, SupplierReturn):
                StockMovement.objects.filter(pk=current.movement_id).update(is_void=True)
                if current.cash_entry_id: CashEntry.objects.filter(pk=current.cash_entry_id).update(is_void=True)
                current.delete()
            elif isinstance(current, Sale):
                current.is_void=True; current.save(update_fields=['is_void'])
                StockMovement.objects.filter(reference__in=(current.number,f'VOID-{current.number}')).update(is_void=True)
                CashEntry.objects.filter(source_sale=current).update(is_void=True)
            elif isinstance(current, Product):
                current.active=False; current.save(update_fields=['active'])
                current.movements.update(is_void=True)
            elif hasattr(current,'is_void'):
                model._default_manager.filter(pk=current.pk).update(is_void=True)
            else:
                current.delete()
            restored=None
        else:
            state=log.before_data
            if not state: raise ValidationError('No previous state is available for this action.')
            values={}
            for field in model._meta.concrete_fields:
                key=field.attname
                if key not in state or field.name=='password': continue
                raw=state[key]
                if raw is not None:
                    target=field.target_field if field.is_relation else field
                    raw=target.to_python(raw)
                values[key]=raw
            pk_field=model._meta.pk.attname
            if current is None:
                values[pk_field]=model._meta.pk.to_python(log.object_id)
            if model is get_user_model() and 'password' not in values and current is None:
                restored=model(**values); restored.set_unusable_password(); restored.save(force_insert=True)
                values=None
            if values is None:
                pass
            elif current:
                values.pop(pk_field,None)
                model._default_manager.filter(pk=current.pk).update(**values)
                restored=model._default_manager.get(pk=current.pk)
            else:
                restored=model(**values); restored.save(force_insert=True)
            if isinstance(restored,Sale):
                StockMovement.objects.filter(reference=restored.number).update(is_void=restored.is_void)
                StockMovement.objects.filter(reference=f'VOID-{restored.number}').update(is_void=not restored.is_void)
                CashEntry.objects.filter(source_sale=restored).update(is_void=restored.is_void)
            if isinstance(restored,InternshipRecord) and log.action=='DELETE':
                payment_logs=ActivityLog.objects.filter(
                    entity_type='INTERNSHIP_PAYMENT',
                    action='VOID',
                    details__startswith='Internship program deleted',
                ).order_by('created_at','pk')
                for payment_log in payment_logs:
                    payment_state=payment_log.before_data
                    if not payment_state or str(payment_state.get('internship_id'))!=str(restored.pk):
                        continue
                    payment_id=InternshipPayment._meta.pk.to_python(payment_log.object_id)
                    if InternshipPayment.objects.filter(pk=payment_id).exists():
                        continue
                    payment_values={}
                    for field in InternshipPayment._meta.concrete_fields:
                        key=field.attname
                        if key not in payment_state: continue
                        raw=payment_state[key]
                        if raw is not None:
                            target=field.target_field if field.is_relation else field
                            raw=target.to_python(raw)
                        payment_values[key]=raw
                    payment_values[InternshipPayment._meta.pk.attname]=payment_id
                    payment_values['is_void']=True
                    cash_entry_id=payment_values.get('cash_entry_id')
                    cash_log=None
                    if cash_entry_id:
                        cash_log=ActivityLog.objects.filter(
                            entity_type='CASHBOOK',
                            object_id=str(cash_entry_id),
                            action='VOID',
                            details__startswith='Internship program deleted',
                        ).order_by('-created_at','-pk').first()
                    if not cash_entry_id or cash_log:
                        payment_values['is_void']=False
                    payment=InternshipPayment(**payment_values)
                    payment.save(force_insert=True)
                    if cash_log and cash_log.before_data:
                        cash_values={}
                        for field in CashEntry._meta.concrete_fields:
                            key=field.attname
                            if key not in cash_log.before_data: continue
                            raw=cash_log.before_data[key]
                            if raw is not None:
                                target=field.target_field if field.is_relation else field
                                raw=target.to_python(raw)
                            cash_values[key]=raw
                        CashEntry.objects.filter(pk=cash_entry_id).update(**cash_values)
                restored.fee_paid=InternshipPayment.objects.filter(
                    internship=restored,
                    is_void=False,
                ).exclude(cash_entry__is_void=True).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
                restored.save(update_fields=['fee_paid'])
            if isinstance(restored,CashEntry) and not restored.is_void:
                linked_purchase=StockPurchase.objects.filter(cash_entry=restored).first()
                linked_payment=StockPurchasePayment.objects.filter(cash_entry=restored).select_related('purchase').first()
                if not linked_purchase and not linked_payment and restored.reference.startswith('PURCHASE-'):
                    try: linked_purchase=StockPurchase.objects.filter(pk=int(restored.reference.split('-',1)[1])).first()
                    except (ValueError,IndexError): pass
                linked_claim=ExpenseClaim.objects.filter(cash_entry=restored).first()
                linked_internship=InternshipPayment.objects.filter(cash_entry=restored).select_related('internship').first()
                linked_return=SupplierReturn.objects.filter(cash_entry=restored).first()
                if linked_purchase:
                    linked_purchase.cash_entry=restored; linked_purchase.payment_method=restored.payment_method; linked_purchase.status='PAID'; linked_purchase.paid_on=restored.date; linked_purchase.save(update_fields=['cash_entry','payment_method','status','paid_on'])
                if linked_payment:
                    linked_payment.purchase.status='PAID' if linked_payment.purchase.amount_due<=0 else 'PENDING'
                    linked_payment.purchase.paid_on=restored.date if linked_payment.purchase.status=='PAID' else None
                    linked_payment.purchase.save(update_fields=['status','paid_on'])
                if linked_claim:
                    linked_claim.status='APPROVED'; linked_claim.reviewed_by=request.user; linked_claim.reviewed_at=timezone.now(); linked_claim.save(update_fields=['status','reviewed_by','reviewed_at'])
                if linked_internship:
                    record=linked_internship.internship
                    linked_internship.is_void=False
                    linked_internship.save(update_fields=['is_void'])
                    record.fee_paid=min(record.fee,record.fee_paid+linked_internship.amount)
                    record.save(update_fields=['fee_paid'])
                if linked_return:
                    linked_return.settlement=restored.payment_method; linked_return.save(update_fields=['settlement'])
        current_after=model._default_manager.filter(pk=log.object_id).first()
        ActivityLog.objects.create(actor=request.user,entity_type=log.entity_type,object_id=log.object_id,object_label=log.object_label,action='UPDATE',details=f'Recovered activity #{log.pk}',before_data=previous_state,after_data=_audit_snapshot(current_after),recovered_from=log)
        messages.success(request,'The previous record state has been recovered.')
    except (ValidationError,ValueError,TypeError,ProtectedError,model.DoesNotExist) as exc:
        transaction.set_rollback(True)
        messages.error(request,f'Recovery failed safely: {exc}')
    return redirect('activity_history')

@module_required('inventory_view')
@transaction.atomic
def product_create(request,pk=None):
    product=get_object_or_404(Product,pk=pk) if pk else None
    if not request.user.is_superuser:
        access=UserModuleAccess.objects.filter(user=request.user).first()
        required='inventory_edit' if product else 'inventory_add'
        if not access or not getattr(access,required): return render(request,'core/forbidden.html',status=403)
    if request.method=='POST':
        try:
            p=product or Product(created_by=request.user)
            p.sku=request.POST.get('sku','').strip(); p.name=request.POST['name'].strip(); p.unit=request.POST.get('unit','unit'); p.reorder_level=Decimal(request.POST.get('reorder_level') or '0'); p.unit_cost=Decimal(request.POST.get('unit_cost') or '0'); p.sale_price=Decimal(request.POST.get('sale_price') or '0')
            image=request.FILES.get('image')
            if image: p.image=image
            p.full_clean(); p.save(); record_audit(request.user,'STOCK_ITEM',p,'UPDATE' if product else 'CREATE','Stock item details saved')
            qty=Decimal(request.POST.get('opening_qty') or '0')
            condition=request.POST.get('condition','FRESH')
            if qty and not product:
                movement=StockMovement.objects.create(product=p,condition=condition,quantity=qty,unit_cost=p.unit_cost,reference='OPENING',notes='Opening stock',created_by=request.user)
                record_audit(request.user,'STOCK_MOVEMENT',movement,'CREATE','Opening stock added')
                amount=Decimal(request.POST.get('purchase_amount') or str(qty*p.unit_cost))
                if amount<=0: raise ValidationError('Enter the total purchase amount for opening stock.')
                record_stock_purchase(movement,amount,request.POST.get('purchase_method','CASH'),request.POST.get('supplier','').strip(),request.user)
            messages.success(request,'Product saved.')
            return redirect('inventory')
        except (KeyError,ValueError,InvalidOperation,ValidationError) as e: transaction.set_rollback(True); messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Please check product fields: {e}')
    return render(request,'core/product_form.html',{'product':product})

@module_required('inventory_view')
def product_image(request,pk):
    product=get_object_or_404(Product,pk=pk)
    if not product.image: raise Http404
    extension=product.image.name.rsplit('.',1)[-1].lower()
    content_type={'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp'}.get(extension,'application/octet-stream')
    return FileResponse(product.image.open('rb'),content_type=content_type,as_attachment=False,filename=product.image.name.rsplit('/',1)[-1])

@module_required('inventory_add')
@transaction.atomic
def stock_move(request):
    if request.method=='POST':
        try:
            p=get_object_or_404(Product,pk=request.POST['product'],active=True,is_archived=False)
            qty=Decimal(request.POST['quantity']); cost=Decimal(request.POST.get('unit_cost') or '0')
            movement=StockMovement.objects.create(product=p,condition=request.POST.get('condition','FRESH'),date=date.fromisoformat(request.POST.get('date','')),quantity=qty,unit_cost=cost,reference=request.POST.get('reference',''),notes=request.POST.get('notes',''),created_by=request.user)
            record_audit(request.user,'STOCK_MOVEMENT',movement,'CREATE','Stock movement added')
            if request.POST.get('is_purchase')=='on':
                if qty<=0: raise ValidationError('A stock purchase must add a positive quantity.')
                amount=Decimal(request.POST.get('purchase_amount') or str(qty*cost))
                if amount<=0: raise ValidationError('Enter the total amount for this stock purchase.')
                record_stock_purchase(movement,amount,request.POST.get('purchase_method','CASH'),request.POST.get('supplier','').strip(),request.user)
            messages.success(request,'Stock movement recorded.')
            return redirect('inventory')
        except (KeyError,ValueError,InvalidOperation,ValidationError) as e: transaction.set_rollback(True); messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Please check movement fields: {e}')
    return render(request,'core/stock_form.html',{'products':Product.objects.filter(active=True,is_archived=False),'today':timezone.localdate()})

@module_required('inventory_view')
def stock_purchases(request):
    status=request.GET.get('status','PENDING')
    purchases=StockPurchase.objects.select_related('product','created_by','movement','cash_entry').prefetch_related('payments__cash_entry','payments__paid_by').all()
    if status in ('PENDING','PAID'): purchases=purchases.filter(status=status)
    else: status='ALL'
    query=request.GET.get('q','').strip()
    if query: purchases=purchases.filter(Q(product__name__icontains=query)|Q(product__sku__icontains=query)|Q(supplier__icontains=query)|Q(movement__reference__icontains=query))
    purchase_rows=[]
    for purchase in purchases[:300]:
        purchase_rows.append({'purchase':purchase,'paid':purchase.amount_paid,'due':purchase.amount_due})
    pending_due=sum((purchase.amount_due for purchase in StockPurchase.objects.filter(status='PENDING').prefetch_related('payments')),Decimal('0.00'))
    return render(request,'core/stock_purchases.html',{'purchases':purchase_rows,'status':status,'query':query,'pending_total':pending_due,'payment_methods':StockPurchasePayment.METHODS})

@module_required('inventory_edit')
@transaction.atomic
def stock_purchase_pay(request,pk):
    purchase=get_object_or_404(StockPurchase,pk=pk)
    if request.method=='POST' and purchase.status=='PENDING':
        try:
            method=request.POST.get('payment_method','CASH')
            if method not in ('CASH','UPI','BANK'): raise ValidationError('Choose a valid payment method.')
            amount=Decimal(request.POST.get('amount') or '0')
            if amount<=0 or amount>purchase.amount_due: raise ValidationError(f'Payment must be greater than zero and no more than the remaining balance of ₹{purchase.amount_due:.2f}.')
            transaction_id=request.POST.get('transaction_id','').strip()
            proof=request.FILES.get('proof')
            if not transaction_id: raise ValidationError('Enter the transaction ID or payment reference.')
            if not proof: raise ValidationError('Upload a screenshot or image of the payment proof.')
            category,_=CashCategory.objects.get_or_create(name='Stock purchases',entry_type='EXPENSE')
            entry=CashEntry.objects.create(entry_type='EXPENSE',date=timezone.localdate(),category=category,reason=f'Settled stock purchase · {purchase.product.name}',party=purchase.supplier,payment_method=method,amount=amount,reference=f'PURCHASE-{purchase.pk:06d}',created_by=request.user)
            payment=StockPurchasePayment(purchase=purchase,date=timezone.localdate(),amount=amount,payment_method=method,transaction_id=transaction_id,proof=proof,cash_entry=entry,paid_by=request.user)
            payment.full_clean(); payment.save()
            fully_paid=purchase.amount_due<=0
            purchase.status='PAID' if fully_paid else 'PENDING'; purchase.paid_on=timezone.localdate() if fully_paid else None; purchase.save(update_fields=['status','paid_on'])
            record_audit(request.user,'STOCK_PURCHASE',purchase,'UPDATE',f'Payment of ₹{amount:.2f} recorded by {method.lower()}; remaining balance ₹{purchase.amount_due:.2f}')
            messages.success(request,f'Payment of ₹{amount:.2f} recorded for {purchase.product.name}; remaining balance ₹{purchase.amount_due:.2f}.')
        except (ValueError,InvalidOperation,ValidationError) as e: transaction.set_rollback(True); messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Could not pay purchase: {e}')
    elif purchase.status=='PAID': messages.info(request,'This purchase is already marked paid.')
    return redirect('stock_purchases')

@module_required('inventory_view')
def stock_purchase_proof(request,pk):
    payment=get_object_or_404(StockPurchasePayment,pk=pk)
    return FileResponse(payment.proof.open('rb'),content_type='image/'+payment.proof.name.rsplit('.',1)[-1].lower().replace('jpg','jpeg'))

@module_required('inventory_edit')
@transaction.atomic
def stock_movement_edit(request,pk):
    original=get_object_or_404(StockMovement,pk=pk,is_void=False)
    if StockPurchase.objects.filter(movement=original).exists():
        messages.error(request,'Purchase-linked stock is locked to preserve its supplier and payment history.')
        return redirect(f'/inventory/?condition={original.condition}')
    if original.reference.startswith(('SALE-','VOID-')):
        messages.error(request,'Sales-linked stock is managed from the sales log.')
        return redirect(f'/inventory/?condition={original.condition}')
    if request.method=='POST':
        try:
            product=get_object_or_404(Product,pk=request.POST['product'],active=True,is_archived=False)
            qty=Decimal(request.POST['quantity']); condition=request.POST.get('condition','FRESH')
            StockMovement.objects.filter(pk=original.pk).update(is_void=True)
            record_audit(request.user,'STOCK_MOVEMENT',original,'UPDATE','Previous movement superseded')
            replacement=StockMovement.objects.create(product=product,condition=condition,date=date.fromisoformat(request.POST['date']),quantity=qty,unit_cost=Decimal(request.POST.get('unit_cost') or '0'),reference=request.POST.get('reference',''),notes=request.POST.get('notes',''),created_by=request.user)
            record_audit(request.user,'STOCK_MOVEMENT',replacement,'UPDATE','Replacement movement recorded')
            if original.product.stock_on_hand(original.condition)<0 or product.stock_on_hand(condition)<0: raise ValidationError('This change would make stock negative. Adjust later stock first.')
            messages.success(request,'Stock movement updated; the previous entry remains in history.'); return redirect(f'/inventory/?condition={condition}')
        except (KeyError,ValueError,InvalidOperation,ValidationError) as e:
            transaction.set_rollback(True); messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Check movement details: {e}')
    return render(request,'core/stock_form.html',{'products':Product.objects.filter(active=True,is_archived=False),'today':timezone.localdate(),'movement':original,'editing':True})

@module_required('inventory_delete')
@transaction.atomic
def stock_movement_void(request,pk):
    movement=get_object_or_404(StockMovement,pk=pk,is_void=False)
    if request.method=='POST':
        if StockPurchase.objects.filter(movement=movement).exists():
            messages.error(request,'Purchase-linked stock is locked to preserve its supplier and payment history.')
        elif movement.reference.startswith(('SALE-','VOID-')):
            messages.error(request,'Sales-linked stock is managed from the sales log.')
        else:
            StockMovement.objects.filter(pk=movement.pk).update(is_void=True)
            if movement.product.stock_on_hand(movement.condition)<0:
                transaction.set_rollback(True); messages.error(request,'Cannot remove this stock entry because later sales use that quantity.')
            else: messages.success(request,'Stock entry removed from current totals and kept in history.')
    return redirect(f'/inventory/?condition={movement.condition}')

@superadmin_required
@transaction.atomic
def product_archive(request,pk):
    if request.method=='POST':
        p=get_object_or_404(Product,pk=pk,is_archived=False)
        p.is_archived=True
        p.save(update_fields=['is_archived'])
        record_audit(request.user,'STOCK_ITEM',p,'ARCHIVE','Item archived; history retained')
        messages.success(request,'Product temporarily archived. You can restore it from Archived stock.')
    return redirect('/inventory/?status=ARCHIVED')

@superadmin_required
@transaction.atomic
def product_restore(request,pk):
    if request.method=='POST':
        p=get_object_or_404(Product,pk=pk,is_archived=True)
        p.is_archived=False
        p.save(update_fields=['is_archived'])
        record_audit(request.user,'STOCK_ITEM',p,'UPDATE','Archived item restored; active status retained')
        messages.success(request,'Product restored. Its previous active or inactive status was retained.')
        return redirect(f'/inventory/?status={"ACTIVE" if p.active else "INACTIVE"}')
    return redirect('/inventory/?status=ARCHIVED')

@superadmin_required
@transaction.atomic
def product_toggle_active(request,pk):
    if request.method=='POST':
        p=get_object_or_404(Product,pk=pk,is_archived=False)
        before=_audit_snapshot(p)
        p.active=not p.active
        p.save(update_fields=['active'])
        record_audit(request.user,'STOCK_ITEM',p,'UPDATE','Product activated' if p.active else 'Product deactivated',before_data=before)
        messages.success(request,f'Product marked {"active" if p.active else "inactive"}.')
        return redirect(f'/inventory/?status={"ACTIVE" if p.active else "INACTIVE"}')
    return redirect('inventory')

@superadmin_required
@transaction.atomic
def product_delete(request,pk):
    if request.method=='POST':
        p=get_object_or_404(Product,pk=pk,is_archived=True)
        product_id=p.pk
        label=str(p)
        try:
            p.delete()
        except ProtectedError:
            messages.error(request,'This item has stock, sale, purchase, or return history and cannot be permanently deleted. It remains archived.')
        else:
            p.pk=product_id
            record_audit(request.user,'STOCK_ITEM',p,'DELETE','Archived product permanently deleted')
            messages.success(request,f'{label} was permanently deleted.')
    return redirect('/inventory/?status=ARCHIVED')

@module_required('cashbook_view')
def cashbook(request):
    all_entries=CashEntry.objects.select_related('category','created_by','stock_purchase','stock_purchase_payment','expense_claim','internship_payment')
    entries=all_entries
    query=request.GET.get('q','').strip()
    if query: entries=entries.filter(Q(reason__icontains=query)|Q(reference__icontains=query)|Q(party__icontains=query)|Q(payment_method__icontains=query)|Q(category__name__icontains=query))
    kind=request.GET.get('type')
    if kind in ('INCOME','EXPENSE'): entries=entries.filter(entry_type=kind)
    active_entries=all_entries.filter(is_void=False)
    income=sum((e.amount for e in active_entries.filter(entry_type='INCOME')),Decimal('0.00'))
    expenses=sum((e.amount for e in active_entries.filter(entry_type='EXPENSE')),Decimal('0.00'))
    return render(request,'core/cashbook.html',{'entries':entries.select_related('created_by','category')[:200],'income':income,'expenses':expenses,'net':income-expenses,'kind':kind,'query':query})

@module_required('cashbook_view')
def expense_claims(request):
    claims=ExpenseClaim.objects.select_related('submitted_by','reviewed_by','cash_entry').all()
    if not request.user.is_superuser: claims=claims.filter(submitted_by=request.user)
    status=request.GET.get('status','')
    if status in dict(ExpenseClaim.STATUSES): claims=claims.filter(status=status)
    query=request.GET.get('q','').strip()
    if query: claims=claims.filter(Q(reason__icontains=query)|Q(vendor__icontains=query)|Q(reference__icontains=query)|Q(submitted_by__username__icontains=query))
    return render(request,'core/expense_claims.html',{'claims':claims,'status':status,'query':query,'statuses':ExpenseClaim.STATUSES})

@module_required('cashbook_add')
@transaction.atomic
def expense_claim_create(request):
    if request.method=='POST':
        try:
            claim=ExpenseClaim(date=date.fromisoformat(request.POST['date']),category=request.POST['category'],reason=request.POST['reason'].strip(),amount=Decimal(request.POST['amount']),vendor=request.POST.get('vendor','').strip(),reference=request.POST.get('reference','').strip(),receipt=request.FILES.get('receipt'),submitted_by=request.user)
            claim.full_clean(); claim.save(); record_audit(request.user,'EXPENSE_CLAIM',claim,'CREATE','Expense claim submitted for review')
            messages.success(request,'Expense claim submitted for superadmin approval.'); return redirect('expense_claims')
        except (KeyError,ValueError,InvalidOperation,ValidationError) as e:
            transaction.set_rollback(True); messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Check claim details: {e}')
    return render(request,'core/expense_claim_form.html',{'categories':ExpenseClaim.CATEGORIES,'today':timezone.localdate()})

@superadmin_required
@transaction.atomic
def expense_claim_review(request,pk):
    claim=get_object_or_404(ExpenseClaim,pk=pk)
    if request.method=='POST' and claim.status=='PENDING':
        decision=request.POST.get('decision')
        if decision not in ('APPROVED','REJECTED'):
            messages.error(request,'Choose approve or reject for this claim.')
        else:
            claim.status=decision; claim.reviewer_note=request.POST.get('reviewer_note','').strip(); claim.reviewed_by=request.user; claim.reviewed_at=timezone.now()
            if decision=='APPROVED':
                category,_=CashCategory.objects.get_or_create(name=claim.get_category_display(),entry_type='EXPENSE')
                entry=CashEntry.objects.create(entry_type='EXPENSE',date=claim.date,category=category,reason=claim.reason,party=claim.vendor or str(claim.submitted_by or ''),payment_method='OTHER',amount=claim.amount,reference=claim.reference or f'CLAIM-{claim.pk:06d}',created_by=claim.submitted_by)
                claim.cash_entry=entry
            claim.save(update_fields=['status','reviewer_note','reviewed_by','reviewed_at','cash_entry'])
            record_audit(request.user,'EXPENSE_CLAIM',claim,'UPDATE',f'Expense claim {decision.lower()}')
            messages.success(request,f'Claim {decision.lower()}.')
    return redirect('expense_claims')

@module_required('cashbook_view')
def expense_claim_receipt(request,pk):
    claim=get_object_or_404(ExpenseClaim,pk=pk)
    if not request.user.is_superuser and claim.submitted_by_id!=request.user.pk:
        return render(request,'core/forbidden.html',status=403)
    if not claim.receipt: raise Http404
    ext=claim.receipt.name.rsplit('.',1)[-1].lower(); return FileResponse(claim.receipt.open('rb'),content_type={'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp'}.get(ext,'application/octet-stream'))

@module_required('cashbook_view')
@transaction.atomic
def cash_entry_form(request, pk=None):
    entry=get_object_or_404(CashEntry,pk=pk,is_void=False) if pk else None
    purchase=StockPurchase.objects.filter(cash_entry=entry).first() if entry else None
    purchase_payment=StockPurchasePayment.objects.filter(cash_entry=entry).select_related('purchase').first() if entry else None
    claim=ExpenseClaim.objects.filter(cash_entry=entry).first() if entry else None
    internship_payment=InternshipPayment.objects.filter(cash_entry=entry).select_related('internship').first() if entry else None
    supplier_return=SupplierReturn.objects.filter(cash_entry=entry).first() if entry else None
    sale=Sale.objects.filter(book_entries=entry).first() if entry else None
    linked=bool(purchase or purchase_payment or claim or internship_payment or supplier_return or sale)
    if linked and not request.user.is_superuser:
        messages.error(request,'Only a superuser can edit an automatically generated transaction.')
        return redirect('cashbook')
    if not request.user.is_superuser:
        access=UserModuleAccess.objects.filter(user=request.user).first()
        required='cashbook_edit' if entry else 'cashbook_add'
        if not access or not getattr(access,required): return render(request,'core/forbidden.html',status=403)
    if request.method=='POST':
        try:
            before=_audit_snapshot(entry) if entry else None
            is_new=entry is None
            source_before=None; source_entity=''
            if purchase: source_before=_audit_snapshot(purchase); source_entity='STOCK_PURCHASE'
            elif purchase_payment: source_before=_audit_snapshot(purchase_payment); source_entity='STOCK_PURCHASE_PAYMENT'
            elif claim: source_before=_audit_snapshot(claim); source_entity='EXPENSE_CLAIM'
            elif internship_payment: source_before=_audit_snapshot(internship_payment); source_entity='INTERNSHIP_PAYMENT'
            elif supplier_return: source_before=_audit_snapshot(supplier_return); source_entity='SUPPLIER_RETURN'
            elif sale: source_before=_audit_snapshot(sale); source_entity='SALE'
            kind=entry.entry_type if linked else request.POST['entry_type']
            category=get_object_or_404(CashCategory,pk=request.POST['category'],active=True,entry_type=kind)
            obj=entry or CashEntry(created_by=request.user)
            entry_date=date.fromisoformat(request.POST['date'])
            amount=Decimal(request.POST.get('amount') or '0')
            method=request.POST.get('payment_method','CASH')
            party=request.POST.get('party','').strip()
            reason=request.POST.get('reason','').strip()
            reference=request.POST.get('reference','').strip()
            if amount<=0: raise ValidationError('Transaction amount must be greater than zero.')
            if sale and amount!=sale.total: raise ValidationError('Sale amount is controlled by the sale lines; edit this sale from the Sales page.')
            allowed_methods=(('CASH','Cash'),('UPI','UPI'),('BANK','Bank transfer')) if purchase or purchase_payment or supplier_return else Sale.PAYMENT_TYPES if sale else CashEntry.PAYMENT_METHODS
            if method not in dict(allowed_methods): raise ValidationError('Choose a payment method supported by the linked record.')
            obj.entry_type=kind; obj.date=entry_date; obj.category=category
            obj.reason=reason; obj.party=party; obj.payment_method=method; obj.amount=amount; obj.reference=reference
            obj.full_clean()
            if purchase:
                purchase.date=entry_date; purchase.supplier=party; purchase.amount=amount; purchase.payment_method=method; purchase.status='PAID'; purchase.paid_on=entry_date; purchase.full_clean(); purchase.save()
            elif purchase_payment:
                parent=purchase_payment.purchase
                if amount>parent.amount_due+purchase_payment.amount: raise ValidationError('Payment amount exceeds the remaining purchase balance.')
                purchase_payment.date=entry_date; purchase_payment.amount=amount; purchase_payment.payment_method=method; purchase_payment.full_clean(); purchase_payment.save()
                parent.status='PAID' if parent.amount_due<=0 else 'PENDING'; parent.paid_on=entry_date if parent.status=='PAID' else None; parent.save(update_fields=['status','paid_on'])
            elif claim:
                claim.date=entry_date; claim.amount=amount; claim.reason=reason; claim.vendor=party
                category_keys={label:key for key,label in ExpenseClaim.CATEGORIES}
                if category.name in category_keys: claim.category=category_keys[category.name]
                claim.full_clean(); claim.save(update_fields=['date','amount','reason','vendor','category'])
            elif internship_payment:
                parent=internship_payment.internship
                new_paid=parent.fee_paid-internship_payment.amount+amount
                if new_paid<0 or new_paid>parent.fee: raise ValidationError('The edited payment would put the internship total outside the allowed fee.')
                internship_payment.date=entry_date; internship_payment.amount=amount; internship_payment.payment_method=method; internship_payment.reference=reference; internship_payment.full_clean(); internship_payment.save()
                parent.fee_paid=new_paid; parent.save(update_fields=['fee_paid'])
            elif supplier_return:
                supplier_return.date=entry_date; supplier_return.amount=amount; supplier_return.supplier=party; supplier_return.settlement=method; supplier_return.full_clean(); supplier_return.save(update_fields=['date','amount','supplier','settlement'])
            elif sale:
                sale.date=entry_date; sale.customer_name=party; sale.payment_type=method; sale.save(update_fields=['date','customer_name','payment_type'])
                StockMovement.objects.filter(reference=sale.number).update(date=entry_date)
            if entry:
                CashEntry.objects.filter(pk=entry.pk).update(entry_type=obj.entry_type,date=obj.date,category=obj.category,reason=obj.reason,party=obj.party,payment_method=obj.payment_method,amount=obj.amount,reference=obj.reference)
                obj.refresh_from_db()
            else: obj.save()
            record_audit(request.user,'CASHBOOK',obj,'CREATE' if is_new else 'UPDATE','Cashbook entry saved',before_data=before)
            if linked:
                source_obj=purchase or purchase_payment or claim or internship_payment or supplier_return or sale
                record_audit(request.user,source_entity,source_obj,'UPDATE','Linked transaction updated from cashbook',before_data=source_before)
            messages.success(request,'Transaction saved and its linked record updated.'); return redirect('cashbook')
        except (KeyError,ValueError,InvalidOperation,ValidationError) as e: messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Please check the entry: {e}')
    methods=(('CASH','Cash'),('UPI','UPI'),('BANK','Bank transfer')) if purchase or purchase_payment or supplier_return else Sale.PAYMENT_TYPES if sale else CashEntry.PAYMENT_METHODS
    return render(request,'core/cash_entry_form.html',{'entry':entry,'categories':CashCategory.objects.filter(active=True,entry_type=entry.entry_type if entry else None) if entry else CashCategory.objects.filter(active=True),'payment_methods':methods,'today':timezone.localdate(),'linked':linked,'amount_locked':bool(sale),'source_label':'Sale total is maintained from the sale lines.' if sale else ''})

@module_required('cashbook_delete')
@transaction.atomic
def cash_entry_delete(request,pk):
    if request.method=='POST':
        entry=get_object_or_404(CashEntry,pk=pk,is_void=False)
        purchase=StockPurchase.objects.filter(cash_entry=entry).first()
        purchase_payment=StockPurchasePayment.objects.filter(cash_entry=entry).select_related('purchase').first()
        claim=ExpenseClaim.objects.filter(cash_entry=entry).first()
        internship_payment=InternshipPayment.objects.filter(cash_entry=entry).select_related('internship').first()
        supplier_return=SupplierReturn.objects.filter(cash_entry=entry).first()
        linked=bool(entry.source_sale_id or purchase or purchase_payment or claim or internship_payment or supplier_return)
        if linked and request.user.is_superuser:
            before=_audit_snapshot(entry)
            CashEntry.objects.filter(pk=entry.pk).update(is_void=True)
            details='Auto-generated cashbook entry removed by superuser; linked history retained'
            if purchase:
                purchase.cash_entry=None; purchase.payment_method='CREDIT'; purchase.status='PENDING'; purchase.paid_on=None
                purchase.save(update_fields=['cash_entry','payment_method','status','paid_on'])
            if purchase_payment:
                parent=purchase_payment.purchase; parent.status='PENDING'; parent.paid_on=None; parent.save(update_fields=['status','paid_on'])
            if claim:
                claim.status='REJECTED'; claim.reviewer_note='Approved expense entry removed by superuser.'; claim.reviewed_by=request.user; claim.reviewed_at=timezone.now()
                claim.save(update_fields=['status','reviewer_note','reviewed_by','reviewed_at'])
            if internship_payment:
                internship=internship_payment.internship
                internship.fee_paid=max(Decimal('0.00'),internship.fee_paid-internship_payment.amount)
                internship.save(update_fields=['fee_paid'])
                before_payment=_audit_snapshot(internship_payment)
                internship_payment.is_void=True
                internship_payment.save(update_fields=['is_void'])
                record_audit(request.user,'INTERNSHIP_PAYMENT',internship_payment,'VOID','Linked cashbook entry removed; internship payment marked void',before_data=before_payment)
            if supplier_return:
                supplier_return.settlement='CREDIT'; supplier_return.save(update_fields=['settlement'])
            record_audit(request.user,'CASHBOOK',entry,'VOID',details,before_data=before)
            messages.success(request,'Generated cashbook entry removed from active totals; related records and history were retained.')
        elif linked:
            messages.error(request,'Only a superuser can remove an automatically generated entry.')
        else:
            record_audit(request.user,'CASHBOOK',entry,'DELETE','Cashbook entry deleted'); entry.delete(); messages.success(request,'Book entry deleted.')
    return redirect('cashbook')

@module_required('sales_add')
@transaction.atomic
def sale_create(request):
    products=list(Product.objects.filter(active=True,is_archived=False).order_by('sku'))
    for product in products:
        product.available_quantity=product.stock_on_hand()
    if request.method=='POST':
        try:
            sale_date=date.fromisoformat(request.POST.get('date',''))
            payment_type=request.POST.get('payment_type','CASH')
            if payment_type not in dict(Sale.PAYMENT_TYPES): raise ValidationError('Choose a valid payment type.')
            customer_name=request.POST.get('customer_name','').strip()
            customer_address=request.POST.get('customer_address','').strip()
            customer_phone=request.POST.get('customer_phone','').strip()
            customer_email=request.POST.get('customer_email','').strip()
            notes=request.POST.get('notes','').strip()
            if customer_email:
                from django.core.validators import validate_email
                validate_email(customer_email)
            subtotal=Decimal('0.00'); items=[]
            for i in range(1,51):
                product_id=request.POST.get(f'product_{i}'); qty=Decimal(request.POST.get(f'quantity_{i}') or '0')
                if not product_id or qty==0: continue
                product=Product.objects.filter(pk=product_id,active=True,is_archived=False).first()
                if not product: raise ValidationError('Choose a valid active item for each sale line.')
                price=Decimal(request.POST.get(f'price_{i}') or '0')
                line_discount=Decimal(request.POST.get(f'discount_{i}') or '0')
                if qty<=0 or price<=0 or line_discount<0: raise ValidationError('Quantity and sale price must be positive; item discount cannot be negative.')
                gross=qty*price
                if line_discount>gross: raise ValidationError(f'Item discount cannot exceed the line total for {product.name}.')
                items.append((product,qty,price,line_discount)); subtotal+=gross
            if not items: raise ValidationError('Add at least one item to the sale.')
            requested={}
            for product,qty,price,line_discount in items: requested[product.pk]=requested.get(product.pk,Decimal('0'))+qty
            for product,qty,price,line_discount in items:
                available=product.stock_on_hand()
                if requested[product.pk]>available:
                    raise ValidationError(f'Not enough total stock for {product.name}: requested {requested[product.pk]:g}, but only {available:g} is available.')
            discount=sum((item[3] for item in items),Decimal('0.00'))
            total=subtotal-discount
            sale=Sale.objects.create(date=sale_date,customer_name=customer_name,customer_address=customer_address,customer_phone=customer_phone,customer_email=customer_email,payment_type=payment_type,notes=notes,created_by=request.user)
            sale.number=f'SALE-{sale.date:%Y%m%d}-{sale.pk:06d}'; sale.subtotal=subtotal; sale.discount=discount; sale.total=total; sale.save(update_fields=['number','subtotal','discount','total'])
            for product,qty,price,line_discount in items:
                remaining=qty; remaining_discount=line_discount
                for stock_condition in ('FRESH','USED'):
                    available=product.stock_on_hand(stock_condition)
                    taken=min(remaining,available)
                    if taken<=0: continue
                    allocated_discount=remaining_discount if taken==remaining else (line_discount*taken/qty).quantize(Decimal('0.01'))
                    SaleLine.objects.create(sale=sale,product=product,condition=stock_condition,quantity=taken,unit_price=price,discount=allocated_discount)
                    StockMovement.objects.create(product=product,condition=stock_condition,date=sale.date,quantity=-taken,unit_cost=product.unit_cost,reference=sale.number,notes=f'Sale to {sale.customer_name or "walk-in customer"}',created_by=request.user)
                    remaining-=taken; remaining_discount-=allocated_discount
            category,_=CashCategory.objects.get_or_create(name='Product sales',entry_type='INCOME')
            if total>0: CashEntry.objects.create(entry_type='INCOME',date=sale.date,category=category,reason=f'{sale.number} product sale'+(f' (discount ₹{discount:.2f})' if discount else ''),party=sale.customer_name or 'Walk-in customer',payment_method=payment_type,amount=total,reference=sale.number,created_by=request.user,source_sale=sale)
            record_audit(request.user,'SALE',sale,'CREATE',f'Sale subtotal ₹{subtotal:.2f}, discount ₹{discount:.2f}, paid ₹{total:.2f}')
            messages.success(request,f'Sale {sale.number} saved for ₹{total:.2f}.'); return redirect('sales')
        except (KeyError,TypeError,ValueError,InvalidOperation,ValidationError,IntegrityError) as e:
            transaction.set_rollback(True)
            messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else ('A database constraint rejected this sale. Check the item, date, and sale details, then try again.' if isinstance(e,IntegrityError) else f'Please check sale details: {e}'))
    return render(request,'core/sale_form.html',{'products':products,'today':timezone.localdate(),'payment_types':Sale.PAYMENT_TYPES,'form_data':request.POST if request.method=='POST' else {}})

@module_required('sales_view')
def sales_list(request):
    query=request.GET.get('q','').strip()
    sales=Sale.objects.all()
    if query: sales=sales.filter(Q(number__icontains=query)|Q(customer_name__icontains=query)|Q(customer_address__icontains=query)|Q(customer_phone__icontains=query)|Q(customer_email__icontains=query)|Q(notes__icontains=query)|Q(lines__product__name__icontains=query)|Q(lines__product__sku__icontains=query)).distinct()
    return render(request,'core/sales.html',{'sales':sales.select_related('created_by').prefetch_related('lines__product')[:200],'query':query,'sales_total':Sale.objects.filter(is_void=False).aggregate(v=Sum('total'))['v'] or 0,'sale_count':Sale.objects.filter(is_void=False).count()})

@module_required('sales_view')
def sale_bill(request,pk):
    sale=get_object_or_404(Sale.objects.select_related('created_by').prefetch_related('lines__product'),pk=pk)
    return render(request,'core/sale_bill.html',{'sale':sale})

@module_required('sales_edit')
def sale_edit(request,pk):
    sale=get_object_or_404(Sale,pk=pk,is_void=False)
    if request.method=='POST':
        before=_audit_snapshot(sale)
        sale.customer_name=request.POST.get('customer_name','').strip(); sale.customer_address=request.POST.get('customer_address','').strip(); sale.customer_phone=request.POST.get('customer_phone','').strip(); sale.customer_email=request.POST.get('customer_email','').strip(); sale.notes=request.POST.get('notes','').strip(); sale.payment_type=request.POST.get('payment_type','CASH'); sale.save(update_fields=['customer_name','customer_address','customer_phone','customer_email','notes','payment_type'])
        sale.book_entries.filter(entry_type='INCOME').update(party=sale.customer_name or 'Walk-in customer',payment_method=sale.payment_type)
        record_audit(request.user,'SALE',sale,'UPDATE','Sale customer, payment type, or notes updated',before_data=before)
        messages.success(request,'Sale details updated; item, quantity and price records are retained.')
        return redirect('sales')
    return render(request,'core/sale_edit.html',{'sale':sale,'payment_types':Sale.PAYMENT_TYPES})

@module_required('sales_delete')
@transaction.atomic
def sale_void(request,pk):
    if request.method=='POST':
        sale=get_object_or_404(Sale,pk=pk)
        if sale.is_void: messages.error(request,'This sale is already voided.')
        else:
            before=_audit_snapshot(sale)
            for line in sale.lines.select_related('product'):
                StockMovement.objects.create(product=line.product,condition=line.condition,date=timezone.localdate(),quantity=line.quantity,unit_cost=line.product.unit_cost,reference=f'VOID-{sale.number}',notes=f'Stock restored for voided sale {sale.number}',created_by=request.user)
            CashEntry.objects.filter(source_sale=sale).update(is_void=True)
            record_audit(request.user,'SALE',sale,'VOID','Sale voided and stock restored',before_data=before)
            sale.is_void=True; sale.save(update_fields=['is_void']); messages.success(request,f'Sale {sale.number} voided and stock restored.')
    return redirect('sales')

@superadmin_required
def superadmin_home(request): return render(request,'core/superadmin.html',{'users':get_user_model().objects.order_by('username'),'products':Product.objects.count(),'entries':CashEntry.objects.count(),'assets':BusinessAsset.objects.count(),'services':ServiceRecord.objects.count()})

@superadmin_required
def backup_excel(request):
    import re
    from io import BytesIO
    from zipfile import ZIP_DEFLATED, ZipFile
    from .exports import make_xlsx
    mode=request.GET.get('mode','')
    if mode not in ('same','new','both'):
        return render(request,'core/backup_choice.html')
    sheets=[]; used_titles=set(); max_rows=1048576
    def sheet_title(raw):
        base=re.sub(r'[\\/*?:\[\]]','_',raw).strip("' ") or 'Data'
        base=base[:31]; title=base; suffix=2
        while title.casefold() in used_titles:
            tail=f'_{suffix}'; title=base[:31-len(tail)]+tail; suffix+=1
        used_titles.add(title.casefold())
        return title
    for config in apps.get_app_configs():
        if config.label in ('sessions','staticfiles'):
            continue
        for model in config.get_models(include_auto_created=True):
            if model._meta.proxy: continue
            fields=[field for field in model._meta.concrete_fields if field.name.lower()!='password']
            base=f'{config.label}_{model._meta.model_name}'
            rows=[[field.name for field in fields]]
            part=1
            for obj in model._default_manager.all().iterator(chunk_size=1000):
                rows.append([getattr(obj,field.attname) for field in fields])
                if len(rows)>=max_rows:
                    title=sheet_title(base if part==1 else f'{base}_{part}')
                    sheets.append((title,rows)); rows=[[field.name for field in fields]]; part+=1
            if len(rows)>1 or part==1:
                title=sheet_title(base if part==1 else f'{base}_{part}')
                sheets.append((title,rows))
    if not sheets: sheets=[('Backup',[['No database tables available']])]
    workbook=make_xlsx(sheets)
    stamp=timezone.localtime().strftime('%Y%m%d_%H%M%S')
    if mode=='both':
        bundle=BytesIO()
        with ZipFile(bundle,'w',ZIP_DEFLATED) as archive:
            archive.writestr('starnux_full_backup.xlsx',workbook)
            archive.writestr(f'starnux_full_backup_{stamp}.xlsx',workbook)
        response=HttpResponse(bundle.getvalue(),content_type='application/zip')
        response['Content-Disposition']=f'attachment; filename="starnux_backups_{stamp}.zip"'
        return response
    response=HttpResponse(workbook,content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    filename='starnux_full_backup.xlsx' if mode=='same' else f'starnux_full_backup_{stamp}.xlsx'
    response['Content-Disposition']=f'attachment; filename="{filename}"'
    return response

@superadmin_required
def user_list(request):
    query=request.GET.get('q','').strip()
    users=get_user_model().objects.order_by('username')
    if query: users=users.filter(Q(username__icontains=query)|Q(first_name__icontains=query)|Q(last_name__icontains=query)|Q(email__icontains=query))
    return render(request,'core/users.html',{'users':users.prefetch_related('module_access'),'query':query})

@superadmin_required
def user_toggle_active(request,pk):
    user=get_object_or_404(get_user_model(),pk=pk)
    if request.method=='POST':
        if user.is_superuser:
            messages.error(request,'Superuser accounts cannot be deactivated here.')
        elif user.pk==request.user.pk:
            messages.error(request,'You cannot deactivate your own account.')
        else:
            before=_audit_snapshot(user)
            user.is_active=not user.is_active; user.save(update_fields=['is_active'])
            record_audit(request.user,'USER',user,'UPDATE','User account '+('activated' if user.is_active else 'deactivated'),before_data=before)
            messages.success(request,f'{user.username} account '+('activated.' if user.is_active else 'deactivated. Existing sessions will be blocked.'))
    return redirect('manage_users')

@superadmin_required
def user_edit(request,pk=None):
    User=get_user_model(); user=get_object_or_404(User,pk=pk) if pk else None
    access=UserModuleAccess.objects.filter(user=user).first() if user else None
    if request.method=='POST':
        try:
            username=request.POST['username'].strip()
            if User.objects.exclude(pk=user.pk if user else None).filter(username=username).exists(): raise ValidationError('That username is already in use.')
            was_superuser=user.is_superuser if user else False
            user_before=_audit_snapshot(user) if user else None
            is_new=user is None
            user=user or User()
            user.username=username; user.first_name=request.POST.get('first_name','').strip(); user.last_name=request.POST.get('last_name','').strip(); user.email=request.POST.get('email','').strip(); user.is_active=request.POST.get('is_active')=='on'; user.is_superuser=request.POST.get('is_superuser')=='on'; user.is_staff=user.is_superuser
            if user.pk == request.user.pk and (not user.is_superuser or not user.is_active): raise ValidationError('You cannot remove your own superadmin access or deactivate your account.')
            if was_superuser and not user.is_active: raise ValidationError('Superuser accounts cannot be deactivated here.')
            password=request.POST.get('password','')
            if password: validate_password(password,user); user.set_password(password)
            elif not user.pk: raise ValidationError('Set a password for the new user.')
            user.save(); access,_=UserModuleAccess.objects.get_or_create(user=user)
            access_before=_audit_snapshot(access) if access.pk and not is_new else None
            access_is_new=access_before is None
            access_fields=('cashbook_view','cashbook_add','cashbook_edit','cashbook_delete','inventory_view','inventory_add','inventory_edit','inventory_delete','sales_view','sales_add','sales_edit','sales_delete','reports_view','assets_view','assets_add','assets_edit','assets_delete','services_view','services_add','services_edit','services_delete','internships_view','internships_add','internships_edit','internships_delete')
            for field in access_fields: setattr(access,field,request.POST.get(field)=='on')
            for section in ('cashbook','inventory','sales','assets','services','internships'):
                actions=('add','edit','delete')
                if any(getattr(access,f'{section}_{action}') for action in actions): setattr(access,f'{section}_view',True)
            access.save(); record_audit(request.user,'USER',user,'CREATE' if is_new else 'UPDATE','User account saved',before_data=user_before); record_audit(request.user,'USER_ACCESS',access,'CREATE' if access_is_new else 'UPDATE','User module permissions saved',before_data=access_before); messages.success(request,'User and module access saved.'); return redirect('manage_users')
        except (KeyError,ValidationError) as e: messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else 'Enter a username.')
    return render(request,'core/user_form.html',{'managed_user':user,'access':access})

@superadmin_required
def user_delete(request,pk):
    user=get_object_or_404(get_user_model(),pk=pk)
    if user.is_superuser: messages.error(request,'Superuser accounts cannot be deleted here.')
    elif request.method=='POST':
        access=UserModuleAccess.objects.filter(user=user).first()
        if access: record_audit(request.user,'USER_ACCESS',access,'DELETE','Module access removed with user account')
        record_audit(request.user,'USER',user,'DELETE','User account deleted'); user.delete(); messages.success(request,'User deleted.')
    return redirect('manage_users')

@superadmin_required
def category_list(request):
    query=request.GET.get('q','').strip()
    categories=CashCategory.objects.order_by('entry_type','name')
    if query: categories=categories.filter(name__icontains=query)
    return render(request,'core/categories.html',{'categories':categories,'query':query})

@superadmin_required
def category_edit(request,pk=None):
    category=get_object_or_404(CashCategory,pk=pk) if pk else None
    if request.method=='POST':
        try:
            before=_audit_snapshot(category) if category else None
            is_new=category is None
            obj=category or CashCategory()
            obj.name=request.POST['name'].strip(); obj.entry_type=request.POST['entry_type']; obj.active=request.POST.get('active')=='on'; obj.full_clean(); obj.save()
            record_audit(request.user,'CATEGORY',obj,'CREATE' if is_new else 'UPDATE','Cashbook category saved',before_data=before)
            messages.success(request,'Category saved.'); return redirect('manage_categories')
        except (KeyError,ValidationError) as e: messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else 'Enter a category name.')
    return render(request,'core/category_form.html',{'category':category,'types':CashCategory.TYPES})

@superadmin_required
def category_delete(request,pk):
    from django.db.models.deletion import ProtectedError
    if request.method=='POST':
        category=get_object_or_404(CashCategory,pk=pk)
        try: record_audit(request.user,'CATEGORY',category,'DELETE','Cashbook category deleted'); category.delete(); messages.success(request,'Category deleted.')
        except ProtectedError: messages.error(request,'This category is used by book entries. Deactivate it instead.')
    return redirect('manage_categories')

@module_required('assets_view')
def asset_list(request):
    archived=request.GET.get('status')=='RETIRED'
    assets=BusinessAsset.objects.filter(status='RETIRED') if archived else BusinessAsset.objects.exclude(status='RETIRED')
    assets=assets.prefetch_related('images')
    for asset in assets:
        asset.image_count=len(asset.images.all())+int(bool(asset.screenshot))
    query=request.GET.get('q','').strip()
    if query: assets=assets.filter(Q(asset_tag__icontains=query)|Q(name__icontains=query)|Q(brand_model__icontains=query)|Q(serial_number__icontains=query)|Q(assigned_to__icontains=query)|Q(location__icontains=query))
    active_assets=BusinessAsset.objects.exclude(status='RETIRED')
    asset_totals=active_assets.aggregate(value=Sum('purchase_cost'))
    return render(request,'core/assets.html',{'assets':assets,'archived':archived,'query':query,'asset_count':active_assets.count(),'asset_value':asset_totals['value'] or Decimal('0.00')})

@module_required('assets_view')
def asset_form(request,pk=None):
    asset=get_object_or_404(BusinessAsset,pk=pk) if pk else None
    if not request.user.is_superuser:
        access=UserModuleAccess.objects.filter(user=request.user).first(); permission='assets_edit' if asset else 'assets_add'
        if not access or not getattr(access,permission): return render(request,'core/forbidden.html',status=403)
    if request.method=='POST':
        try:
            before=_audit_snapshot(asset) if asset else None
            is_new=asset is None
            obj=asset or BusinessAsset(created_by=request.user)
            obj.asset_tag=request.POST['asset_tag'].strip(); obj.name=request.POST['name'].strip(); obj.asset_type=request.POST['asset_type']; obj.brand_model=request.POST.get('brand_model','').strip(); obj.serial_number=request.POST.get('serial_number','').strip(); obj.condition=request.POST['condition']; obj.status=request.POST['status']; obj.purchase_date=date.fromisoformat(request.POST['purchase_date']) if request.POST.get('purchase_date') else None; obj.purchase_cost=Decimal(request.POST.get('purchase_cost') or '0'); obj.assigned_to=request.POST.get('assigned_to','').strip(); obj.location=request.POST.get('location','').strip(); obj.license_expiry=date.fromisoformat(request.POST['license_expiry']) if request.POST.get('license_expiry') else None; obj.notes=request.POST.get('notes','')
            upload=request.FILES.get('screenshot')
            if upload: obj.screenshot=upload
            uploads=request.FILES.getlist('images')
            existing_images=asset.images.count() if asset else 0
            existing_images+=int(bool((asset and asset.screenshot) or upload))
            if existing_images+len(uploads)>5: raise ValidationError(f'An asset can have at most 5 images. This asset already has {existing_images}; remove some or upload fewer.')
            obj.full_clean(); obj.save(); record_audit(request.user,'ASSET',obj,'CREATE' if is_new else 'UPDATE','Asset record saved',before_data=before)
            for upload in uploads:
                image=BusinessAssetImage(asset=obj,image=upload,uploaded_by=request.user)
                image.full_clean(); image.save()
                record_audit(request.user,'ASSET_IMAGE',image,'CREATE','Asset image uploaded')
            messages.success(request,'Asset record saved.'); return redirect('assets')
        except (KeyError,ValueError,InvalidOperation,ValidationError) as e: messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Check asset details: {e}')
    return render(request,'core/asset_form.html',{'asset':asset,'types':BusinessAsset.TYPES,'conditions':BusinessAsset.CONDITIONS,'statuses':BusinessAsset.STATUSES,'image_count':(asset.images.count()+int(bool(asset.screenshot))) if asset else 0})

@module_required('assets_view')
def asset_image_attachment(request,pk):
    image=get_object_or_404(BusinessAssetImage,pk=pk)
    extension=image.image.name.rsplit('.',1)[-1].lower()
    content_type={'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp'}.get(extension,'application/octet-stream')
    return FileResponse(image.image.open('rb'),content_type=content_type,as_attachment=False,filename=image.image.name.rsplit('/',1)[-1])

@module_required('assets_delete')
def asset_archive(request,pk):
    if request.method=='POST':
        asset=get_object_or_404(BusinessAsset,pk=pk); before=_audit_snapshot(asset); asset.status='RETIRED'; asset.save(update_fields=['status']); record_audit(request.user,'ASSET',asset,'ARCHIVE','Asset retired',before_data=before); messages.success(request,'Asset retired; record and attachment are retained.')
    return redirect('assets')

@module_required('services_view')
def services(request):
    rows=ServiceRecord.objects.all()
    status=request.GET.get('status')
    if status in dict(ServiceRecord.STATUSES): rows=rows.filter(status=status)
    query=request.GET.get('q','').strip()
    if query: rows=rows.filter(Q(number__icontains=query)|Q(title__icontains=query)|Q(customer_name__icontains=query)|Q(contact__icontains=query)|Q(description__icontains=query))
    return render(request,'core/services.html',{'records':rows.prefetch_related('images'),'status':status,'statuses':ServiceRecord.STATUSES,'query':query})

@module_required('internships_view')
def internships(request):
    rows=InternshipRecord.objects.select_related('created_by').prefetch_related('payments').all()
    status=request.GET.get('status')
    if status in dict(InternshipRecord.STATUSES): rows=rows.filter(status=status)
    query=request.GET.get('q','').strip()
    if query: rows=rows.filter(Q(number__icontains=query)|Q(name__icontains=query)|Q(email__icontains=query)|Q(phone__icontains=query)|Q(program__icontains=query)|Q(mentor__icontains=query))
    return render(request,'core/internships.html',{'records':rows,'statuses':InternshipRecord.STATUSES,'status':status,'query':query})

@module_required('internships_add')
def internship_create(request): return internship_form(request)

@module_required('internships_edit')
def internship_edit(request,pk): return internship_form(request,pk)

@module_required('internships_edit')
@transaction.atomic
def internship_payment_create(request,pk):
    record=get_object_or_404(InternshipRecord,pk=pk)
    if request.method=='POST':
        try:
            amount=Decimal(request.POST.get('amount') or '0')
            if amount<=0 or record.fee_paid+amount>record.fee: raise ValidationError('Payment must be positive and cannot exceed the remaining internship fee.')
            method=request.POST.get('payment_method','CASH')
            if method not in dict(CashEntry.PAYMENT_METHODS): raise ValidationError('Choose a valid payment method.')
            paid_date=date.fromisoformat(request.POST.get('date') or timezone.localdate().isoformat())
            payment=InternshipPayment(internship=record,date=paid_date,amount=amount,payment_method=method,reference=request.POST.get('reference','').strip(),received_by=request.user)
            payment.full_clean(); payment.save()
            record.fee_paid+=amount; record.save(update_fields=['fee_paid'])
            category,_=CashCategory.objects.get_or_create(name='Internship fees',entry_type='INCOME')
            entry=CashEntry.objects.create(entry_type='INCOME',date=paid_date,category=category,reason=f'Internship fee payment · {record.number}',party=record.name,payment_method=method,amount=amount,reference=payment.reference or f'INTPAY-{payment.pk:06d}',created_by=request.user)
            payment.cash_entry=entry; payment.save(update_fields=['cash_entry'])
            record_audit(request.user,'INTERNSHIP',record,'UPDATE',f'Fee payment of ₹{amount:.2f} recorded for {paid_date}')
            messages.success(request,f'Payment of ₹{amount:.2f} recorded for {record.number}.')
            return redirect('internships')
        except (ValueError,InvalidOperation,ValidationError) as e:
            transaction.set_rollback(True); messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Check payment details: {e}')
    return render(request,'core/internship_payment_form.html',{'record':record,'balance_due':record.fee-record.fee_paid,'today':timezone.localdate(),'payment_methods':CashEntry.PAYMENT_METHODS})

@transaction.atomic
def internship_form(request,pk=None):
    obj=get_object_or_404(InternshipRecord,pk=pk) if pk else None
    if request.method=='POST':
        try:
            record=obj or InternshipRecord(created_by=request.user)
            before=_audit_snapshot(obj) if obj else None
            record.name=request.POST['name'].strip(); record.email=request.POST.get('email','').strip(); record.phone=request.POST.get('phone','').strip(); record.program=request.POST['program'].strip(); record.mentor=request.POST.get('mentor','').strip(); record.start_date=date.fromisoformat(request.POST['start_date']); record.end_date=date.fromisoformat(request.POST['end_date']) if request.POST.get('end_date') else None; record.status=request.POST['status']; record.fee=Decimal(request.POST.get('fee') or '0'); record.fee_paid=Decimal(request.POST.get('fee_paid') or '0'); record.notes=request.POST.get('notes','').strip()
            active_payments=list(obj.payments.filter(is_void=False).exclude(cash_entry__is_void=True).select_related('cash_entry').order_by('-date','-id')) if obj else []
            active_paid=sum((payment.amount for payment in active_payments),Decimal('0.00'))
            payment_amount=record.fee_paid-active_paid
            method=request.POST.get('payment_method','CASH')
            if payment_amount and method not in dict(CashEntry.PAYMENT_METHODS): raise ValidationError('Choose a valid payment method.')
            payment_date=date.fromisoformat(request.POST.get('payment_date') or timezone.localdate().isoformat()) if payment_amount else None
            record.full_clean(); record.save()
            if payment_amount<0:
                remaining_reduction=-payment_amount
                for payment in active_payments:
                    reduction=min(payment.amount,remaining_reduction)
                    before_payment=_audit_snapshot(payment)
                    new_amount=payment.amount-reduction
                    if new_amount==0:
                        payment.is_void=True
                        payment.save(update_fields=['is_void'])
                        if payment.cash_entry_id:
                            cash_entry=payment.cash_entry
                            before_entry=_audit_snapshot(cash_entry)
                            CashEntry.objects.filter(pk=cash_entry.pk).update(is_void=True)
                            record_audit(request.user,'CASHBOOK',cash_entry,'VOID','Internship payment reduced to zero',before_data=before_entry)
                    else:
                        payment.amount=new_amount
                        payment.save(update_fields=['amount'])
                        if payment.cash_entry_id:
                            cash_entry=payment.cash_entry
                            before_entry=_audit_snapshot(cash_entry)
                            cash_entry.amount=new_amount
                            cash_entry.party=record.name
                            cash_entry.save(update_fields=['amount','party'])
                            record_audit(request.user,'CASHBOOK',cash_entry,'UPDATE','Internship payment amount adjusted from internship record',before_data=before_entry)
                    record_audit(request.user,'INTERNSHIP_PAYMENT',payment,'VOID' if new_amount==0 else 'UPDATE','Internship payment adjusted from fee paid total',before_data=before_payment)
                    remaining_reduction-=reduction
                    if remaining_reduction==0: break
            if payment_amount:
                if payment_amount>0:
                    payment=InternshipPayment(internship=record,date=payment_date,amount=payment_amount,payment_method=method,reference=request.POST.get('payment_reference','').strip(),received_by=request.user)
                    payment.full_clean(); payment.save()
                    category,_=CashCategory.objects.get_or_create(name='Internship fees',entry_type='INCOME')
                    entry=CashEntry.objects.create(entry_type='INCOME',date=payment_date,category=category,reason=f'Internship fee payment · {record.number}',party=record.name,payment_method=method,amount=payment_amount,reference=payment.reference or f'INTPAY-{payment.pk:06d}',created_by=request.user)
                    payment.cash_entry=entry; payment.save(update_fields=['cash_entry'])
            record_audit(request.user,'INTERNSHIP',record,'UPDATE' if obj else 'CREATE','Internship record saved',before_data=before); messages.success(request,f'Internship {record.number} saved.'); return redirect('internships')
        except (KeyError,ValueError,InvalidOperation,ValidationError) as e: transaction.set_rollback(True); messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Check internship details: {e}')
    return render(request,'core/internship_form.html',{'record':obj,'statuses':InternshipRecord.STATUSES,'today':timezone.localdate()})

@module_required('internships_delete')
@transaction.atomic
def internship_delete(request,pk):
    if request.method=='POST':
        record=get_object_or_404(InternshipRecord,pk=pk)
        record_audit(request.user,'INTERNSHIP',record,'DELETE','Internship record deleted; linked payments voided in cashbook')
        for payment in record.payments.select_related('cash_entry').all():
            if payment.cash_entry_id and not payment.cash_entry.is_void:
                cash_entry=payment.cash_entry
                before_entry=_audit_snapshot(cash_entry)
                CashEntry.objects.filter(pk=cash_entry.pk).update(is_void=True)
                record_audit(request.user,'CASHBOOK',cash_entry,'VOID','Internship program deleted; payment removed from active cashbook totals',before_data=before_entry)
            before_payment=_audit_snapshot(payment)
            payment.is_void=True
            payment.save(update_fields=['is_void'])
            record_audit(request.user,'INTERNSHIP_PAYMENT',payment,'VOID','Internship program deleted',before_data=before_payment)
            payment.delete()
        record.delete()
        messages.success(request,'Internship deleted. Any linked payments were voided from cashbook totals and retained as voided entries.')
    return redirect('internships')

@module_required('services_add')
@transaction.atomic
def service_create(request):
    if request.method=='POST':
        try:
            obj=ServiceRecord(title=request.POST['title'].strip(),service_type=request.POST['service_type'],customer_name=request.POST.get('customer_name','').strip(),contact=request.POST.get('contact','').strip(),description=request.POST.get('description',''),status=request.POST.get('status','PENDING'),opened_date=date.fromisoformat(request.POST['opened_date']),due_date=date.fromisoformat(request.POST['due_date']) if request.POST.get('due_date') else None,completed_date=date.fromisoformat(request.POST['completed_date']) if request.POST.get('completed_date') else None,charge=Decimal(request.POST.get('charge') or '0'),created_by=request.user,screenshot=request.FILES.get('screenshot'))
            obj.full_clean(); obj.save(); record_audit(request.user,'SERVICE',obj,'CREATE','Service record created')
            for upload in request.FILES.getlist('images'):
                image=ServiceImage(service=obj,image=upload,uploaded_by=request.user); image.full_clean(); image.save()
            messages.success(request,f'Service {obj.number} added.'); return redirect('services')
        except (KeyError,ValueError,InvalidOperation,ValidationError) as e: transaction.set_rollback(True); messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Check service details: {e}')
    return render(request,'core/service_form.html',{'types':ServiceRecord.TYPES,'statuses':ServiceRecord.STATUSES,'today':timezone.localdate()})

@module_required('services_edit')
@transaction.atomic
def service_edit(request,pk):
    obj=get_object_or_404(ServiceRecord,pk=pk)
    if request.method=='POST':
        try:
            before=_audit_snapshot(obj)
            obj.title=request.POST['title'].strip(); obj.service_type=request.POST['service_type']; obj.customer_name=request.POST.get('customer_name','').strip(); obj.contact=request.POST.get('contact','').strip(); obj.description=request.POST.get('description',''); obj.status=request.POST['status']; obj.opened_date=date.fromisoformat(request.POST['opened_date']); obj.due_date=date.fromisoformat(request.POST['due_date']) if request.POST.get('due_date') else None; obj.completed_date=date.fromisoformat(request.POST['completed_date']) if request.POST.get('completed_date') else None; obj.charge=Decimal(request.POST.get('charge') or '0')
            upload=request.FILES.get('screenshot')
            if upload: obj.screenshot=upload
            obj.full_clean(); obj.save()
            for upload in request.FILES.getlist('images'):
                image=ServiceImage(service=obj,image=upload,uploaded_by=request.user); image.full_clean(); image.save()
            record_audit(request.user,'SERVICE',obj,'UPDATE','Service record updated',before_data=before); messages.success(request,'Service record updated.'); return redirect('services')
        except (KeyError,ValueError,InvalidOperation,ValidationError) as e: transaction.set_rollback(True); messages.error(request,'; '.join(e.messages) if hasattr(e,'messages') else f'Check service details: {e}')
    return render(request,'core/service_form.html',{'record':obj,'types':ServiceRecord.TYPES,'statuses':ServiceRecord.STATUSES,'today':timezone.localdate()})

@module_required('services_delete')
def service_delete(request,pk):
    if request.method=='POST':
        obj=get_object_or_404(ServiceRecord,pk=pk)
        record_audit(request.user,'SERVICE',obj,'DELETE','Service record deleted'); obj.delete(); messages.success(request,'Service record deleted.')
    return redirect('services')

@module_required('services_view')
def service_attachment(request,pk):
    record=get_object_or_404(ServiceRecord,pk=pk)
    if not record.screenshot: raise Http404
    return FileResponse(record.screenshot.open('rb'),as_attachment=False,filename=record.screenshot.name.rsplit('/',1)[-1])

@module_required('services_view')
def service_image_attachment(request,pk):
    image=get_object_or_404(ServiceImage,pk=pk)
    extension=image.image.name.rsplit('.',1)[-1].lower()
    content_type={'jpg':'image/jpeg','jpeg':'image/jpeg','png':'image/png','webp':'image/webp'}.get(extension,'application/octet-stream')
    return FileResponse(image.image.open('rb'),content_type=content_type,as_attachment=False,filename=image.image.name.rsplit('/',1)[-1])

@module_required('assets_view')
def asset_attachment(request,pk):
    asset=get_object_or_404(BusinessAsset,pk=pk)
    if not asset.screenshot: raise Http404
    return FileResponse(asset.screenshot.open('rb'),as_attachment=False,filename=asset.screenshot.name.rsplit('/',1)[-1])
