from __future__ import annotations

import datetime
import time
from datetime import timedelta

import requests
from loguru import logger

from config import Chains
from core.bot import Bot
from utils.inputs import (
    cell_date_to_txt, get_date_from_txt,
    input_pause, input_cycle_amount, input_cycle_pause, start_pause,
)
from utils.logging import init_logger
from utils.utils import get_accounts, random_sleep, select_and_shuffle_profiles, get_user_agent, prepare_proxy_requests

# ============================================================
SITE_KEY       = '0x4AAAAAAC1zzx2qbnnQLhip'
FAUCET_PAGE    = 'https://faucet.xenea.io/'
FAUCET_API_URL = 'https://ubusuna-faucet-api.xenea.io/faucet'
CAPTCHA_FIELD  = 'token'

COOLDOWN_MINUTES = 720   # 12 часов
MAX_RETRIES      = 3
FILE_FAUCET_DATE = 'faucet_date.txt'
# ============================================================


# ---------------------------------------------------------------------------
# Решение Cloudflare Turnstile через 2Captcha
# ---------------------------------------------------------------------------

def _solve_turnstile(account, timeout: int = 120) -> str | None:
    from config.settings import config as cfg
    api_key = cfg.two_captcha_token
    if not api_key:
        logger.error('TWO_CAPTCHA_TOKEN не задан в .env!')
        return None

    headers = {'User-Agent': account.user_agent}
    proxies = prepare_proxy_requests(account.proxy)

    # Отправить задачу
    resp = requests.post(
        'https://2captcha.com/in.php',
        data={
            'key':     api_key,
            'method':  'turnstile',
            'sitekey': SITE_KEY,
            'pageurl': FAUCET_PAGE,
            'json':    1,
        },
        headers=headers,
        proxies=proxies,
        timeout=20,
    )
    result = resp.json()
    if result.get('status') != 1:
        logger.error(f'{account.profile_number} 2Captcha: ошибка отправки задачи: {result}')
        return None

    task_id = result['request']
    logger.info(f'{account.profile_number} 2Captcha задача #{task_id}, ждём решения...')

    for _ in range(timeout // 5):
        time.sleep(5)
        poll = requests.get(
            'https://2captcha.com/res.php',
            params={'key': api_key, 'action': 'get', 'id': task_id, 'json': 1},
            headers=headers,
            proxies=proxies,
            timeout=15,
        ).json()

        if poll.get('status') == 1:
            logger.success(f'{account.profile_number} 2Captcha Turnstile решена 🎯')
            return poll['request']

        if poll.get('request') != 'CAPCHA_NOT_READY':
            logger.error(f'{account.profile_number} 2Captcha ошибка: {poll}')
            return None

    logger.error(f'{account.profile_number} 2Captcha: таймаут ожидания')
    return None


# ---------------------------------------------------------------------------
# Фильтр аккаунтов — кулдаун 12 часов
# ---------------------------------------------------------------------------

def accounts_filter(accounts) -> list:
    result = []
    limit_date = datetime.datetime.now() - timedelta(minutes=COOLDOWN_MINUTES)
    for acc in accounts:
        last = get_date_from_txt(acc, FILE_FAUCET_DATE)
        if last and last > limit_date:
            next_run = last + timedelta(minutes=COOLDOWN_MINUTES)
            logger.info(
                f'{acc.profile_number} Фосет уже получен {last:%Y-%m-%d %H:%M}, '
                f'следующий запуск после {next_run:%Y-%m-%d %H:%M}'
            )
            continue
        result.append(acc)
    return result


# ---------------------------------------------------------------------------
# Одна попытка получить фосет
# ---------------------------------------------------------------------------

def _do_claim(bot: Bot) -> bool:
    captcha_token = _solve_turnstile(bot.account)
    if not captcha_token:
        return False

    # EVM сеть — берём адрес из столбца Address
    address = bot.account.address
    payload = {
        'address':      address,
        CAPTCHA_FIELD:  captcha_token,
    }
    headers = {
        'Content-Type': 'application/json',
        'User-Agent':   bot.account.user_agent,
        'Origin':       'https://faucet.xenea.io',
        'Referer':      'https://faucet.xenea.io/',
    }
    proxies = prepare_proxy_requests(bot.account.proxy)

    resp = requests.post(
        FAUCET_API_URL,
        json=payload,
        headers=headers,
        proxies=proxies,
        timeout=30,
    )

    if resp.status_code == 200:
        logger.success(f'{bot.account.profile_number} Фосет получен: {resp.text[:200]} 🎯')
        return True

    logger.error(
        f'{bot.account.profile_number} Ошибка фосета [{resp.status_code}]: {resp.text[:300]}'
    )
    return False


# ---------------------------------------------------------------------------
# Получение фосета с retry (до MAX_RETRIES попыток)
# ---------------------------------------------------------------------------

def claim_faucet(bot: Bot) -> bool:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if _do_claim(bot):
                return True
            logger.warning(
                f'{bot.account.profile_number} Попытка {attempt}/{MAX_RETRIES} — неудача, повтор...'
            )
        except Exception as e:
            logger.error(
                f'{bot.account.profile_number} Попытка {attempt}/{MAX_RETRIES} — ошибка: {e}'
            )
        if attempt < MAX_RETRIES:
            random_sleep(5, 10)

    logger.error(
        f'{bot.account.profile_number} Фосет не получен после {MAX_RETRIES} попыток'
    )
    return False


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------

def worker(account) -> None:
    account.user_agent = get_user_agent()  # один UA на весь запуск аккаунта

    with Bot(account, chain=Chains.XENEA_TESTNET) as bot:
        success = claim_faucet(bot)
        if success:
            cell_date_to_txt(bot, FILE_FAUCET_DATE)
            logger.success(f'Фосет завершён 🔥')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    init_logger()
    accounts     = get_accounts()
    accounts     = select_and_shuffle_profiles(accounts)
    pause        = input_pause()
    cycle_amount = input_cycle_amount()
    cycle_pause  = input_cycle_pause()
    delay        = start_pause()

    if delay:
        random_sleep(delay)

    for cycle in range(cycle_amount):
        active = accounts_filter(accounts)
        if not active:
            logger.warning('Все аккаунты уже получили фосет — кулдаун не истёк!')
            break

        logger.info(f'Активных аккаунтов для фосета: {len(active)}')

        for account in active:
            worker(account)
            random_sleep(pause)

        logger.success(f'Цикл {cycle + 1}/{cycle_amount} завершён ✅')
        if cycle < cycle_amount - 1:
            random_sleep(cycle_pause)




if __name__ == '__main__':
    main()
