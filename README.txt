ShopMate Billing V6

This prototype provides a phone-friendly billing dashboard, barcode
scanning with a Raspberry Pi CSI camera, a sample product catalog, and
UPI checkout QR generation. Inventory is committed only after the
prototype "Payment Done" confirmation.

Run on Raspberry Pi:

    cd shopmate_billing_v6
    export SHOPMATE_UPI_PAYEE_ID="your-upi-id@bank"
    export SHOPMATE_UPI_PAYEE_NAME="Your Payee Name"
    python billing_dashboard.py --port 8082

Optional:

    export SHOPMATE_UPI_AID="your-aid"

The QR is generated dynamically from the configured payee and server-side
cart total. No personal UPI details or QR image are included in this repo.
Checkout remains unavailable until SHOPMATE_UPI_PAYEE_ID is configured.

"Payment Done" is a manual prototype confirmation, not bank verification.
A production deployment must verify payments through a payment gateway.
