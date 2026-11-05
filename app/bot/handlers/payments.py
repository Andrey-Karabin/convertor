"""Buying operations with Telegram Stars (currency XTR).

Telegram requires bots that sell digital goods to accept Stars only, and to
answer /terms and /paysupport. Flow: invoice -> pre_checkout_query (we confirm
within 10 s) -> successful_payment (we credit the balance).
"""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery

from app import texts
from app.bot import keyboards
from app.bot.callbacks import BuyCb
from app.config import Settings
from app.services.billing import Billing

router = Router(name="payments")
log = logging.getLogger(__name__)


async def _show_products(message: Message, settings: Settings) -> None:
    if not settings.billing_enabled or not settings.products:
        await message.answer(texts.NO_PRODUCTS)
        return
    await message.answer(f"<b>{texts.BUY_TITLE}</b>\n\n{texts.BUY_INTRO}",
                         reply_markup=keyboards.products(settings.products))


@router.message(Command("buy"))
async def buy_command(message: Message, settings: Settings) -> None:
    await _show_products(message, settings)


@router.callback_query(BuyCb.filter())
async def buy(callback: CallbackQuery, callback_data: BuyCb, bot: Bot, billing: Billing, settings: Settings) -> None:
    await callback.answer()
    if callback_data.code == "menu":
        await _show_products(callback.message, settings)
        return
    product = billing.product(callback_data.code)
    if product is None or not settings.billing_enabled:
        await callback.message.answer(texts.PAYMENT_UNKNOWN)
        return
    title = texts.product_title(product.kind, product.amount)
    await bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=title,
        description=texts.product_description(product.kind, product.amount),
        payload=product.code,
        currency="XTR",  # Telegram Stars; no payment provider needed
        prices=[LabeledPrice(label=title, amount=product.stars)],
    )


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery, billing: Billing) -> None:
    product = billing.product(query.invoice_payload)
    if product is None or query.currency != "XTR" or query.total_amount != product.stars:
        await query.answer(ok=False, error_message=texts.PAYMENT_UNKNOWN)
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def paid(message: Message, billing: Billing, settings: Settings) -> None:
    payment = message.successful_payment
    product = billing.product(payment.invoice_payload)
    if product is None:  # price list changed between invoice and payment
        log.error("Payment for unknown product %s, charge %s, user %s",
                  payment.invoice_payload, payment.telegram_payment_charge_id, message.from_user.id)
        await message.answer(texts.paysupport(settings.support_contact))
        return
    await billing.apply_payment(message.from_user.id, payment.telegram_payment_charge_id, product)
    log.info("Payment: user %s bought %s for %s stars", message.from_user.id, product.code, product.stars)
    balance = await billing.balance(message.from_user.id)
    await message.answer(texts.PAYMENT_THANKS.format(balance=texts.balance_text(balance, settings.billing_enabled)))


@router.message(Command("terms"))
async def terms(message: Message) -> None:
    await message.answer(texts.TERMS)


@router.message(Command("paysupport"))
async def paysupport(message: Message, settings: Settings) -> None:
    await message.answer(texts.paysupport(settings.support_contact))
