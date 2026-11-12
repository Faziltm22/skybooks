from .models import UserModuleAccess

def user_access(request):
    user=request.user
    fields=['cashbook_view','cashbook_add','cashbook_edit','cashbook_delete','inventory_view','inventory_add','inventory_edit','inventory_delete','sales_view','sales_add','sales_edit','sales_delete','reports_view','assets_view','assets_add','assets_edit','assets_delete','services_view','services_add','services_edit','services_delete','internships_view','internships_add','internships_edit','internships_delete']
    if not user.is_authenticated: return {'module_access': None}
    if user.is_superuser: return {'module_access': {field:True for field in fields}}
    return {'module_access': UserModuleAccess.objects.filter(user=user).first()}
