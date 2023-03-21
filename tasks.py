import asyncio
import json

import httpx
from loguru import logger

from lnbits.core.crud import update_payment_extra
from lnbits.core.models import Payment
from lnbits.helpers import get_current_extension_name
from lnbits.tasks import register_invoice_listener
from websocket import WebSocketApp
from lnbits.settings import settings
from .crud import get_pay_link
from threading import Thread


async def wait_for_paid_invoices():
    invoice_queue = asyncio.Queue()
    register_invoice_listener(invoice_queue, get_current_extension_name())

    while True:
        payment = await invoice_queue.get()
        await on_invoice_paid(payment)


async def on_invoice_paid(payment: Payment):
    if payment.extra.get("tag") != "lnurlp":
        return

    if payment.extra.get("wh_status"):
        # this webhook has already been sent
        return

    pay_link = await get_pay_link(payment.extra.get("link", -1))
    if pay_link and pay_link.webhook_url:
        async with httpx.AsyncClient() as client:
            try:
                r: httpx.Response = await client.post(
                    pay_link.webhook_url,
                    json={
                        "payment_hash": payment.payment_hash,
                        "payment_request": payment.bolt11,
                        "amount": payment.amount,
                        "comment": payment.extra.get("comment"),
                        "lnurlp": pay_link.id,
                        "body": json.loads(pay_link.webhook_body)
                        if pay_link.webhook_body
                        else "",
                    },
                    headers=json.loads(pay_link.webhook_headers)
                    if pay_link.webhook_headers
                    else None,
                    timeout=40,
                )
                await mark_webhook_sent(
                    payment.payment_hash,
                    r.status_code,
                    r.is_success,
                    r.reason_phrase,
                    r.text,
                )
            except Exception as ex:
                logger.error(ex)
                await mark_webhook_sent(
                    payment.payment_hash, -1, False, "Unexpected Error", str(ex)
                )

    nostr = payment.extra.get("nostr")
    if nostr:
        from ..nostrclient.nostr.event import Event
        from ..nostrclient.nostr.key import PrivateKey, PublicKey

        event_json = json.loads(nostr)

        def get_tag(event_json, tag):
            res = [
                event_tag[1] for event_tag in event_json["tags"] if event_tag[0] == tag
            ]
            return res[0] if res else None

        private_key = PrivateKey(
            bytes.fromhex(
                "de1af06647137d49b2277faa86f96effc94257a7b7efd6f5dcc52bea08a4746b"
            )
        )

        p_tag = get_tag(event_json, "p")
        tags = []
        for t in ["p", "e"]:
            tag = get_tag(event_json, t)
            if tag:
                tags.append([t, tag])
        tags.append(["bolt11", payment.bolt11])
        tags.append(["description", json.dumps(event_json)])
        zap_receipt = Event(
            public_key="749b4d4dfc6b00a5e6c9a88d8a220c46c069ff8f027dcf312f040475e059554a",
            kind=9735,
            tags=tags,
        )
        private_key.sign_event(zap_receipt)

        print(f"NOSTR STUFF: {event_json}")
        print(f"Receipt: {zap_receipt}")

        def send_event(class_obj):
            ws.send(zap_receipt.to_message())
            # nonlocal wst
            # wst.join(timeout=1)

        ws = WebSocketApp(
            f"wss://localhost:{settings.port}/nostrclient/api/v1/relay",
            on_open=send_event,
        )
        wst = Thread(target=ws.run_forever)
        wst.daemon = True
        wst.start()


async def mark_webhook_sent(
    payment_hash: str, status: int, is_success: bool, reason_phrase="", text=""
) -> None:

    await update_payment_extra(
        payment_hash,
        {
            "wh_status": status,  # keep for backwards compability
            "wh_success": is_success,
            "wh_message": reason_phrase,
            "wh_response": text,
        },
    )
