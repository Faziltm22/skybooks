from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse
from django.test.utils import override_settings
import tempfile

from .models import ActivityLog, BusinessAsset, BusinessAssetImage, CashEntry, InternshipPayment, InternshipRecord, Product, Sale, StockMovement


class ProductLifecycleTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_superuser(
            username='stock-admin',
            email='stock-admin@example.com',
            password='test-password',
        )
        self.client.force_login(self.admin)
        self.product = Product.objects.create(name='Lifecycle test item')

    def test_product_can_be_deactivated_archived_and_restored(self):
        response = self.client.post(reverse('product_toggle_active', args=[self.product.pk]))
        self.assertRedirects(response, '/inventory/?status=INACTIVE')
        self.product.refresh_from_db()
        self.assertFalse(self.product.active)

        self.client.post(reverse('product_archive', args=[self.product.pk]))
        self.product.refresh_from_db()
        self.assertTrue(self.product.is_archived)
        self.assertFalse(self.product.active)
        response = self.client.get(reverse('inventory'), {'status': 'ARCHIVED'})
        self.assertContains(response, self.product.name)

        response = self.client.post(reverse('product_restore', args=[self.product.pk]))
        self.assertRedirects(response, '/inventory/?status=INACTIVE')
        self.product.refresh_from_db()
        self.assertFalse(self.product.is_archived)

    def test_permanent_delete_is_blocked_when_stock_history_exists(self):
        StockMovement.objects.create(
            product=self.product,
            quantity=1,
            unit_cost=10,
            reference='OPENING',
            created_by=self.admin,
        )
        self.client.post(reverse('product_archive', args=[self.product.pk]))
        self.client.post(reverse('product_delete', args=[self.product.pk]))

        self.assertTrue(Product.objects.filter(pk=self.product.pk, is_archived=True).exists())
        self.assertFalse(ActivityLog.objects.filter(
            entity_type='STOCK_ITEM',
            object_id=str(self.product.pk),
            action='DELETE',
        ).exists())

    def test_archived_item_without_history_can_be_permanently_deleted(self):
        self.client.post(reverse('product_archive', args=[self.product.pk]))

        response = self.client.post(reverse('product_delete', args=[self.product.pk]))

        self.assertRedirects(response, '/inventory/?status=ARCHIVED')
        self.assertFalse(Product.objects.filter(pk=self.product.pk).exists())
        self.assertTrue(ActivityLog.objects.filter(
            entity_type='STOCK_ITEM',
            object_id=str(self.product.pk),
            action='DELETE',
        ).exists())

    def test_stock_lifecycle_actions_are_superuser_only(self):
        worker = get_user_model().objects.create_user(
            username='stock-worker',
            password='test-password',
        )
        self.client.force_login(worker)

        for action in ('product_archive', 'product_restore', 'product_toggle_active', 'product_delete'):
            response = self.client.post(reverse(action, args=[self.product.pk]))
            self.assertEqual(response.status_code, 403)

        self.product.refresh_from_db()
        self.assertTrue(self.product.active)
        self.assertFalse(self.product.is_archived)


class ProductImageAndSaleStockTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.temp_dir.name)
        self.settings_override.enable()
        user_model = get_user_model()
        self.admin = user_model.objects.create_superuser(
            username='product-admin',
            email='product-admin@example.com',
            password='test-password',
        )
        self.client.force_login(self.admin)
        self.product = Product.objects.create(name='Ten unit item', sale_price=25)
        StockMovement.objects.create(
            product=self.product,
            quantity=10,
            unit_cost=10,
            reference='OPENING',
            created_by=self.admin,
        )

    def tearDown(self):
        self.settings_override.disable()
        self.temp_dir.cleanup()

    @staticmethod
    def image_upload(name='product.png'):
        return SimpleUploadedFile(name, b'\x89PNG\r\n\x1a\n' + b'\x00' * 8, content_type='image/png')

    def test_product_image_upload_is_saved_and_served(self):
        response = self.client.post(reverse('product_edit', args=[self.product.pk]), {
            'name': self.product.name,
            'unit': 'unit',
            'reorder_level': '0',
            'unit_cost': '10',
            'sale_price': '25',
            'image': self.image_upload(),
        })

        self.assertRedirects(response, reverse('inventory'))
        self.product.refresh_from_db()
        self.assertTrue(self.product.image)
        image_response = self.client.get(reverse('product_image', args=[self.product.pk]))
        self.assertEqual(image_response.status_code, 200)
        self.assertEqual(image_response['Content-Type'], 'image/png')
        image_response.close()

    def test_duplicate_sale_lines_cannot_exceed_available_stock(self):
        response = self.client.post(reverse('sale_create'), {
            'date': '2026-11-06',
            'payment_type': 'CASH',
            'product_1': str(self.product.pk),
            'quantity_1': '6',
            'price_1': '25',
            'discount_1': '0',
            'product_2': str(self.product.pk),
            'quantity_2': '5',
            'price_2': '25',
            'discount_2': '0',
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'requested 11, but only 10 is available')
        self.assertEqual(Sale.objects.count(), 0)
        self.assertEqual(self.product.stock_on_hand(), 10)


class AssetImageTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.temp_dir.name)
        self.settings_override.enable()
        user_model = get_user_model()
        self.admin = user_model.objects.create_superuser(
            username='asset-admin',
            email='asset-admin@example.com',
            password='test-password',
        )
        self.client.force_login(self.admin)

    def tearDown(self):
        self.settings_override.disable()
        self.temp_dir.cleanup()

    @staticmethod
    def image_upload(index):
        return SimpleUploadedFile(
            f'asset-{index}.png',
            b'\x89PNG\r\n\x1a\n' + b'\x00' * 8,
            content_type='image/png',
        )

    def asset_data(self):
        return {
            'asset_tag': 'ASSET-001',
            'name': 'Test equipment',
            'asset_type': 'EQUIPMENT',
            'condition': 'FRESH',
            'status': 'ACTIVE',
        }

    def test_asset_accepts_at_most_five_images(self):
        response = self.client.post(
            reverse('asset_create'),
            {**self.asset_data(), 'images': [self.image_upload(i) for i in range(5)]},
        )

        self.assertRedirects(response, reverse('assets'))
        asset = BusinessAsset.objects.get(asset_tag='ASSET-001')
        self.assertEqual(BusinessAssetImage.objects.filter(asset=asset).count(), 5)
        response = self.client.get(reverse('assets'))
        self.assertContains(response, '5 / 5')

    def test_asset_rejects_six_images(self):
        response = self.client.post(
            reverse('asset_create'),
            {**self.asset_data(), 'images': [self.image_upload(i) for i in range(6)]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'at most 5 images')
        self.assertFalse(BusinessAsset.objects.filter(asset_tag='ASSET-001').exists())


class InternshipPaymentAdjustmentTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.admin = user_model.objects.create_superuser(
            username='internship-admin',
            email='internship-admin@example.com',
            password='test-password',
        )
        self.client.force_login(self.admin)

    def save_internship(self, record=None, fee_paid='4000'):
        data = {
            'name': record.name if record else 'Test intern',
            'program': record.program if record else 'Test program',
            'email': '',
            'phone': '',
            'mentor': '',
            'status': 'ACTIVE',
            'start_date': '2026-11-01',
            'end_date': '',
            'fee': '10000',
            'fee_paid': fee_paid,
            'payment_date': '2026-11-06',
            'payment_method': 'CASH',
            'payment_reference': '',
            'notes': '',
        }
        url = reverse('internship_edit', args=[record.pk]) if record else reverse('internship_create')
        return self.client.post(url, data)

    def test_fee_paid_can_be_reduced_and_cashbook_amount_is_adjusted(self):
        self.save_internship()
        record = InternshipRecord.objects.get(name='Test intern')
        payment = InternshipPayment.objects.get(internship=record)
        cash_entry = payment.cash_entry
        self.assertEqual(cash_entry.amount, 4000)

        response = self.save_internship(record, fee_paid='2000')

        self.assertRedirects(response, reverse('internships'))
        record.refresh_from_db()
        payment.refresh_from_db()
        cash_entry.refresh_from_db()
        self.assertEqual(record.fee_paid, 2000)
        self.assertEqual(payment.amount, 2000)
        self.assertEqual(cash_entry.amount, 2000)
        self.assertFalse(cash_entry.is_void)

    def test_deleting_paid_program_voids_cashbook_entries_and_keeps_audit(self):
        self.save_internship()
        record = InternshipRecord.objects.get(name='Test intern')
        cash_entry = InternshipPayment.objects.get(internship=record).cash_entry

        response = self.client.post(reverse('internship_delete', args=[record.pk]))

        self.assertRedirects(response, reverse('internships'))
        self.assertFalse(InternshipRecord.objects.filter(pk=record.pk).exists())
        cash_entry.refresh_from_db()
        self.assertTrue(cash_entry.is_void)
        self.assertIsNone(CashEntry.objects.filter(is_void=False).aggregate(total=Sum('amount'))['total'])
        self.assertTrue(ActivityLog.objects.filter(
            entity_type='CASHBOOK',
            object_id=str(cash_entry.pk),
            action='VOID',
        ).exists())

    def test_superuser_can_recover_deleted_paid_internship(self):
        self.save_internship()
        record = InternshipRecord.objects.get(name='Test intern')
        original_record_id = record.pk
        original_payment = InternshipPayment.objects.get(internship=record)
        original_payment_id = original_payment.pk
        original_cash_entry = original_payment.cash_entry

        self.client.post(reverse('internship_delete', args=[record.pk]))
        deletion_log = ActivityLog.objects.filter(
            entity_type='INTERNSHIP',
            object_id=str(original_record_id),
            action='DELETE',
        ).latest('pk')

        response = self.client.post(reverse('activity_recover', args=[deletion_log.pk]))

        self.assertRedirects(response, reverse('activity_history'))
        self.assertTrue(
            InternshipRecord.objects.filter(pk=original_record_id, number=record.number).exists()
        )
        self.assertTrue(
            InternshipPayment.objects.filter(
                pk=original_payment_id,
                internship_id=original_record_id,
                is_void=False,
            ).exists()
        )
        original_cash_entry.refresh_from_db()
        self.assertFalse(original_cash_entry.is_void)
