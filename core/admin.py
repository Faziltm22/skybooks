from django.contrib import admin
from .models import Account, ActivityLog, AccountingPeriod, BusinessAsset, BusinessAssetImage, CashCategory, CashEntry, CostCentre, FinancialYear, InternshipPayment, InternshipRecord, Product, Sale, SaleLine, ServiceRecord, StockMovement, UserModuleAccess, Voucher, VoucherLine

class SuperuserOnlyAdminSite(admin.AdminSite):
    site_header='STARNux technical setup'
    site_title='STARNux ERP setup'
    index_title='System configuration'
    def has_permission(self, request):
        return request.user.is_active and request.user.is_superuser

admin_site=SuperuserOnlyAdminSite(name='starnux_admin')

@admin.register(Voucher,site=admin_site)
class VoucherAdmin(admin.ModelAdmin):
    list_display=('number','date','voucher_type','narration','status','created_by')
    list_filter=('status','voucher_type','date')
    readonly_fields=('number','status','posted_at','created_at','created_by')
    def has_delete_permission(self, request, obj=None): return obj is None or obj.status=='DRAFT'

admin_site.register([Account,ActivityLog,AccountingPeriod,BusinessAsset,BusinessAssetImage,CashCategory,CashEntry,CostCentre,FinancialYear,InternshipPayment,InternshipRecord,Product,Sale,SaleLine,ServiceRecord,StockMovement,UserModuleAccess,VoucherLine])
