# STARNux ERP

A Django application for STARNux with a custom business interface, SQLite storage, simple income and expense bookkeeping, fresh/used stock, product sales, and a separate super admin workspace.

## Run locally

Requires Python and the virtual environment from the project directory.

```powershell
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
python manage.py makemigrations core
python manage.py migrate
python manage.py createsuperuser
python manage.py seed_starnux
python manage.py runserver
```

Open http://127.0.0.1:8000/ and sign in with the superuser you created. The custom management page is at `/manage/`. SQLite data is kept in `db.sqlite3`.

If you already have a database, make a backup copy of `db.sqlite3` before running new migrations. The new models add user access rules, book entries, product condition, sales and cash entry void status.

## Main pages

- **Cashbook:** Record income or expense with date, category, reason, amount and reference. The totals show income, expenses and profit/loss. Entries can be edited and deleted where the user has those permissions.
- **Inventory:** Add products, product photos, sale/cost prices and reorder levels, and record opening quantities or later stock movements. Fresh/new and used stock are tracked separately. Sales cannot exceed available stock, including duplicate product lines. Super admins can mark items active/inactive, temporarily archive and restore them, or permanently delete an archived item only when it has no protected stock or transaction history.
- **Stock movements:** Switch between fresh and used stock. Add, edit, or remove movements; edited/deleted movements are retained as voided history. Sales-linked movements are protected from manual changes.
- **Sales log:** Record products sold, condition, quantity, unit selling price, customer and payment type. Sales reduce matching stock and add sale income plus cost of goods sold to the cashbook. Voiding a sale restores stock and marks its book entries as void while retaining the history.
- **Service tracker:** Log repair, drone, training, installation or consulting jobs with pending, in-progress and completed status. Add a screenshot/photo attachment. Users with access can view/add; only a superuser can edit or delete service records.
- **Assets & licences:** Track drones, cameras, laptops, phones, other equipment and software licences in a table. Asset tag, name and type are the required fields; optional advanced details include serial, purchase cost, condition, assignment, location and renewal date. Attach up to five images or receipts per asset.
- **Internships:** Track program fees and advance payments. Editing the recorded paid total adjusts linked cashbook entries; deleting a program voids its linked entries from totals while retaining cashbook and audit history.
- **Reports:** Review cashbook transactions and profit/loss for a date range.
- **Super admin:** Create, edit, deactivate or delete users, promote a trusted user to Super admin, and separately grant View, Add, Edit and Delete rights for cashbook, inventory, sales and assets. Enabling an action automatically enables View for that section. Service view/add/edit/delete can be granted separately; reports remain read-only as calculated output. Manage cashbook categories here too.

Screenshots accept PNG, JPG or WebP up to 8 MB and are served through authenticated pages.

Only a Django superuser can open `/manage/`. Create the first one with `python manage.py createsuperuser`. The built-in Django admin remains available at `/admin/` for technical setup and master data.

The legacy double-entry vouchers and chart of accounts remain in the project for advanced bookkeeping, but the main interface centers on the simpler cashbook workflow. GST, vendor/customer account statements and statutory production accounting controls are not part of this basic workflow.



user 1 : id starnuxfazil
    pass starnux22
 supr user : starnuxadmin
 pass      : starnuxadmin
