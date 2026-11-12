from datetime import date
from django.core.management.base import BaseCommand
from django.utils import timezone
from core.models import Account, CashCategory, CostCentre, FinancialYear, AccountingPeriod

class Command(BaseCommand):
    help='Load STARNux starter company, accounts, cost centres and financial periods.'
    def handle(self,*args,**kwargs):
        for code,name in [('DRONE','Drone Solutions'),('INTERNSHIP','Internship Division')]: CostCentre.objects.get_or_create(code=code,defaults={'name':name})
        for kind,names in {'INCOME':['Drone services','Product sales','Internship fees','Other income'],'EXPENSE':['Salaries','Office rent','Electricity','Internet','Marketing','Travel','Equipment','Training materials','Other expense']}.items():
            for name in names: CashCategory.objects.get_or_create(name=name,entry_type=kind)
        groups={
            'ASSET':['Cash in Hand','Bank Account','Accounts Receivable','Prepaid Expenses','Drone Equipment','Computer Equipment','Accumulated Depreciation','Inventory'],
            'LIABILITY':['Accounts Payable','Outstanding Salaries','Unearned Internship Fees','Business Loans'],
            'EQUITY':['Owner Capital','Owner Drawings','Retained Earnings'],
            'INCOME':['Drone Service Income','Drone Sales Income','Internship Fees','Other Income'],
            'EXPENSE':['Employee Salaries','Office Rent','Electricity Expenses','Internet Expenses','Marketing Expenses','Travel Expenses','Equipment Maintenance','Depreciation Expense','Training Materials','Cost of Goods Sold']}
        n=100
        for typ,names in groups.items():
            for name in names:
                Account.objects.get_or_create(name=name,defaults={'code':str(n),'account_type':typ}); n+=10
        today=timezone.localdate(); start=date(today.year if today.month>=4 else today.year-1,4,1); end=date(start.year+1,3,31)
        fy,_=FinancialYear.objects.get_or_create(name=f'FY {start.year}-{str(start.year+1)[-2:]}',defaults={'start_date':start,'end_date':end})
        months=['April','May','June','July','August','September','October','November','December','January','February','March']
        for i,name in enumerate(months):
            year=start.year if i<9 else start.year+1; month=i+4 if i<9 else i-8
            from calendar import monthrange
            a=date(year,month,1); b=date(year,month,monthrange(year,month)[1])
            AccountingPeriod.objects.get_or_create(financial_year=fy,name=name,defaults={'start_date':a,'end_date':b})
        self.stdout.write(self.style.SUCCESS('STARNux starter data is ready.'))
