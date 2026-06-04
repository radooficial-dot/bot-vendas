import os
import asyncio
import httpx
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
ABACATE_KEY = os.getenv("ABACATE_API_KEY")
NOME_LOJA = os.getenv("NOME_LOJA", "Dom Works")
ABACATE_URL = "https://api.abacatepay.com/v1"

HEADERS = {
    "Authorization": f"Bearer {ABACATE_KEY}",
    "Content-Type": "application/json"
}

# ─────────────────────────────────────────
#  CADASTRE SEUS PRODUTOS AQUI
# ─────────────────────────────────────────
PRODUTOS = {
    1: {
        "nome": "Produto Exemplo 1",
        "preco": 29.90,
        "desc": "Descrição do produto 1",
        "chaves": ["CHAVE-AAAA-0001", "CHAVE-AAAA-0002", "CHAVE-AAAA-0003"]
    },
    2: {
        "nome": "Produto Exemplo 2",
        "preco": 49.90,
        "desc": "Descrição do produto 2",
        "chaves": ["CHAVE-BBBB-0001", "CHAVE-BBBB-0002"]
    },
}
# ─────────────────────────────────────────

# Guarda pedidos pendentes: {user_id: {billing_id, produto_id}}
pedidos_pendentes = {}


async def criar_cobranca(produto: dict, user_id: int) -> dict | None:
    """Cria uma cobrança PIX no AbacatePay e retorna os dados."""
    payload = {
        "amount": int(produto["preco"] * 100),  # em centavos
        "description": f"{NOME_LOJA} - {produto['nome']}",
        "expiresIn": 1800,  # 30 minutos
        "customer": {
            "name": f"Cliente {user_id}",
            "cellphone": "11999999999",
            "email": f"cliente{user_id}@email.com",
            "taxId": "000.000.000-00"
        },
        "methods": ["PIX"]
    }
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(f"{ABACATE_URL}/billing/create", json=payload, headers=HEADERS, timeout=15)
            data = r.json()
            if r.status_code == 200 and data.get("data"):
                return data["data"]
        except Exception as e:
            print(f"Erro AbacatePay: {e}")
    return None


async def verificar_pagamento(billing_id: str) -> bool:
    """Verifica se a cobrança foi paga."""
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(f"{ABACATE_URL}/billing/check?id={billing_id}", headers=HEADERS, timeout=10)
            data = r.json()
            status = data.get("data", {}).get("status", "")
            return status == "PAID"
        except Exception as e:
            print(f"Erro verificação: {e}")
    return False


async def monitorar_pagamento(context, chat_id: int, billing_id: str, produto_id: int, tentativas: int = 0):
    """Fica verificando o pagamento a cada 15s por até 30 min."""
    if tentativas >= 120:  # 120 x 15s = 30 min
        await context.bot.send_message(chat_id, "⏰ Tempo expirado. Faça o pedido novamente com /comprar.")
        pedidos_pendentes.pop(chat_id, None)
        return

    pago = await verificar_pagamento(billing_id)

    if pago:
        produto = PRODUTOS.get(produto_id)
        if produto and produto["chaves"]:
            chave = produto["chaves"].pop(0)
            await context.bot.send_message(
                chat_id,
                f"✅ *Pagamento confirmado!*\n\n"
                f"Produto: *{produto['nome']}*\n"
                f"Sua chave de acesso:\n`{chave}`\n\n"
                f"Obrigado pela compra! 🎉\n_{NOME_LOJA}_",
                parse_mode="Markdown"
            )
        else:
            await context.bot.send_message(chat_id, "✅ Pagamento confirmado! Mas o produto está sem estoque. Entre em contato com o suporte.")
        pedidos_pendentes.pop(chat_id, None)
    else:
        await asyncio.sleep(15)
        await monitorar_pagamento(context, chat_id, billing_id, produto_id, tentativas + 1)


# ─── HANDLERS ───────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = (
        f"👋 Bem-vindo à *{NOME_LOJA}*!\n\n"
        f"📦 /produtos — ver catálogo\n"
        f"🛒 /comprar <id> — fazer pedido\n"
        f"❓ /ajuda — suporte"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def produtos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not PRODUTOS:
        await update.message.reply_text("Sem produtos disponíveis no momento.")
        return
    msg = f"🛒 *Catálogo — {NOME_LOJA}*\n\n"
    for pid, p in PRODUTOS.items():
        estoque = len(p["chaves"])
        status = "✅ Disponível" if estoque > 0 else "❌ Esgotado"
        msg += f"*#{pid} — {p['nome']}*\n"
        msg += f"💰 R$ {p['preco']:.2f} | {status}\n"
        msg += f"_{p['desc']}_\n\n"
    msg += "Para comprar use: /comprar <id>"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def comprar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Informe o ID do produto.\nEx: /comprar 1")
        return

    try:
        pid = int(ctx.args[0])
        prod = PRODUTOS[pid]
    except (ValueError, KeyError):
        await update.message.reply_text("Produto não encontrado. Use /produtos para ver o catálogo.")
        return

    if not prod["chaves"]:
        await update.message.reply_text("❌ Produto esgotado no momento.")
        return

    uid = update.effective_user.id
    await update.message.reply_text("⏳ Gerando cobrança PIX, aguarde...")

    cobranca = await criar_cobranca(prod, uid)

    if not cobranca:
        await update.message.reply_text("❌ Erro ao gerar cobrança. Tente novamente em alguns instantes.")
        return

    billing_id = cobranca.get("id")
    pix_code = cobranca.get("brCode") or cobranca.get("pixCode") or cobranca.get("emv", "")
    qr_url = cobranca.get("brCodeBase64") or cobranca.get("qrCodeUrl", "")

    pedidos_pendentes[uid] = {"billing_id": billing_id, "produto_id": pid}

    msg = (
        f"💳 *Pedido: {prod['nome']}*\n"
        f"Valor: R$ {prod['preco']:.2f}\n\n"
        f"📲 *Código PIX Copia e Cola:*\n`{pix_code}`\n\n"
        f"⏰ Expira em 30 minutos.\n"
        f"O produto será entregue automaticamente após o pagamento!"
    )

    keyboard = [[InlineKeyboardButton("🔄 Verificar pagamento", callback_data=f"verificar_{billing_id}_{pid}")]]

    if qr_url and qr_url.startswith("http"):
        await ctx.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=qr_url,
            caption=msg,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

    asyncio.create_task(monitorar_pagamento(ctx, uid, billing_id, pid))


async def callback_verificar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Verificando...")
    parts = query.data.split("_")
    billing_id = parts[1]
    pid = int(parts[2])

    pago = await verificar_pagamento(billing_id)

    if pago:
        prod = PRODUTOS.get(pid)
        if prod and prod["chaves"]:
            chave = prod["chaves"].pop(0)
            await query.edit_message_caption(
                f"✅ *Pagamento confirmado!*\n\nSua chave:\n`{chave}`\n\nObrigado! 🎉",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_caption("✅ Pago! Produto sem estoque — fale com o suporte.")
    else:
        await query.answer("⏳ Pagamento ainda não identificado. Aguarde alguns segundos.", show_alert=True)


async def ajuda(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"❓ *Suporte {NOME_LOJA}*\n\n"
        "Problemas com sua compra? Entre em contato com o administrador.\n\n"
        "/produtos — ver catálogo\n"
        "/comprar <id> — fazer pedido",
        parse_mode="Markdown"
    )


async def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("produtos", produtos))
    app.add_handler(CommandHandler("comprar", comprar))
    app.add_handler(CommandHandler("ajuda", ajuda))
    app.add_handler(CallbackQueryHandler(callback_verificar, pattern="^verificar_"))
    print(f"✅ Bot {NOME_LOJA} rodando com AbacatePay!")
    async with app:
        await app.start()
        await app.updater.start_polling()
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
