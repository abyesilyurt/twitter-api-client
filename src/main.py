import asyncio
import logging.config
import sys
from enum import Enum, auto
from functools import wraps, partial
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import ujson

from .config.operations import operations
from .log.config import log_config
from .login import Session
from .utils import find_key

logging.config.dictConfig(log_config)
logger = logging.getLogger(__name__)

MAX_IMAGE_FILE_SIZE = 5_242_880
CHUNK_SIZE = 8192

BOLD = '\u001b[1m'
SUCCESS = '\u001b[32m'
WARN = '\u001b[31m'
RESET = '\u001b[0m'

MEDIA = {
    '.mp4': {
        'type': 'video/mp4',
        'category': 'tweet_video'
    },
    '.mov': {
        'type': 'video/quicktime',
        'category': 'tweet_video'
    },
    '.png': {
        'type': 'image/png',
        'category': 'tweet_image'
    },
    '.jpg': {
        'type': 'image/jpeg',
        'category': 'tweet_image'
    },
    '.jpeg': {
        'type': 'image/jpeg',
        'category': 'tweet_image'
    },
}


class Operation(Enum):
    CreateTweet = auto()
    CreateScheduledTweet = auto()
    DeleteTweet = auto()
    UserTweets = auto()
    FavoriteTweet = auto()
    UnfavoriteTweet = auto()
    CreateRetweet = auto()
    DeleteRetweet = auto()

    TweetStats = auto()


def log(fn=None, *, level: int = logging.DEBUG, info: list = None):
    if fn is None:
        return partial(log, level=level, info=info)

    @wraps(fn)
    def wrapper(*args, **kwargs):
        r = fn(*args, **kwargs)
        try:
            if 200 <= r.status_code < 300:
                message = f'[{SUCCESS}SUCCESS{RESET}] ({BOLD}{fn.__name__}{RESET})'
                for k in info:
                    if callable(k):
                        logger.log(level, f'{message}: {k(r)}')
                    else:
                        attr = getattr(r, k)
                        v = attr() if callable(attr) else attr
                        d = {f"{k}": v}
                        logger.log(level, f'{message}: {d}')
            else:
                logger.log(level, f'[{WARN}ERROR{RESET}] ({fn.__name__}) {r.status_code} {r.text}')
        except Exception as e:
            logger.log(level, f'[{WARN}FAILED{RESET}] ({fn.__name__}) {e}')
        return r

    return wrapper


def _get_headers(session: Session) -> dict:
    return {
        'authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
        'accept-encoding': 'gzip, deflate, br',
        'cookie': '; '.join(f'{k}={v}' for k, v in session.cookies.items()),
        'referer': 'https://twitter.com/',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
        'x-csrf-token': session.cookies.get('ct0'),
    }


async def get_status(media_id: str, auth_session: Session, check_after_secs: int = 1):
    url = 'https://upload.twitter.com/i/media/upload.json'
    headers = _get_headers(auth_session)
    params = {'command': 'STATUS', 'media_id': media_id}
    while 1:
        await asyncio.sleep(check_after_secs)
        async with aiohttp.ClientSession(headers=headers) as s:
            async with s.get(url, params=params) as r:
                data = await r.json()
        info = data['processing_info']
        state = info['state']
        if state == 'succeeded':
            logger.debug(f'{media_id}: {SUCCESS}processing complete{RESET}')
            return data
        if state == 'in_progress':
            progress = info["progress_percent"]
            check_after_secs = info.get('check_after_secs', check_after_secs)
            logger.debug(f'{media_id}: upload {progress = }%')
        else:
            logger.debug(f'{media_id}: upload {state = }')


async def upload_media(fname: str, auth_session: Session):
    url = 'https://upload.twitter.com/i/media/upload.json'
    headers = _get_headers(auth_session)
    conn = aiohttp.TCPConnector(limit=0, ssl=False, ttl_dns_cache=69)
    async with aiohttp.ClientSession(headers=headers, connector=conn) as s:
        file = Path(fname)
        total_bytes = file.stat().st_size
        params = {
            'command': 'INIT',
            'total_bytes': total_bytes,
            'media_type': MEDIA[file.suffix]['type'],
            'media_category': MEDIA[file.suffix]['category']
        }
        async with s.post(url, headers=headers, params=params) as r:
            info = await r.json()
            logger.debug(f'{info = }')
            media_id = info['media_id']

        with open(fname, 'rb') as f:
            i = 0
            while chunk := f.read(MAX_IMAGE_FILE_SIZE):  # todo: arbitrary max size for now
                with aiohttp.MultipartWriter('form-data') as mpw:
                    part = mpw.append(chunk)
                    part.set_content_disposition('form-data', name='media', filename='blob')
                    s.cookie_jar.update_cookies(auth_session.cookies)  # csrf cookie/header update
                    r = await s.post(
                        url,
                        data=mpw,
                        headers=headers,
                        params={'command': 'APPEND', 'media_id': media_id, 'segment_index': i}
                    )
                    logger.debug(f'{r.status = }')
                    i += 1

        async with s.post(url, headers=headers,
                          params={'command': 'FINALIZE', 'media_id': media_id, 'allow_async': 'true'}) as r:
            res = await r.json()
            logger.debug(f'{res = }')

        if processing_info := res.get('processing_info', {}):
            state = processing_info.get('state')
            if state == 'pending':
                logger.debug(f'{media_id}: {state}')
                return await get_status(media_id, auth_session, processing_info.get('check_after_secs', 1))
            logger.debug(f'{media_id}: {SUCCESS}upload complete{RESET}')
    return res


@log(level=logging.DEBUG, info=['status_code'])
def add_alt_text(text: str, media_id: int, session: Session):
    params = {"media_id": media_id, "alt_text": {"text": text}}
    url = 'https://api.twitter.com/1.1/media/metadata/create.json'
    r = session.post(url, headers=_get_headers(session), json=params)
    return r


@log(level=logging.DEBUG, info=['status_code', 'json'])
def like_tweet(tweet_id: int, session: Session):
    operation = Operation.FavoriteTweet.name
    qid = operations[operation]['queryId']
    params = operations[operation]
    params['variables']['tweet_id'] = tweet_id
    url = f"https://api.twitter.com/graphql/{qid}/{operation}"
    r = session.post(url, headers=_get_headers(session), json=params)
    logger.debug(f'{tweet_id = }')
    return r


@log(level=logging.DEBUG, info=['status_code', 'json'])
def unlike_tweet(tweet_id: int, session: Session):
    operation = Operation.UnfavoriteTweet.name
    qid = operations[operation]['queryId']
    params = operations[operation]
    params['variables']['tweet_id'] = tweet_id
    url = f"https://api.twitter.com/graphql/{qid}/{operation}"
    r = session.post(url, headers=_get_headers(session), json=params)
    logger.debug(f'{tweet_id = }')
    return r


@log(level=logging.DEBUG, info=['status_code', 'json'])
def create_tweet(text: str, session: Session, media: list[dict | str] = None, **kwargs):
    operation = Operation.CreateTweet.name
    qid = operations[operation]['queryId']
    params = operations[operation]
    params['variables']['tweet_text'] = text
    if media:
        for m in media:
            if isinstance(m, dict):
                media_info = asyncio.run(upload_media(m['file'], session))
                params['variables']['media']['media_entities'].append({
                    'media_id': media_info['media_id'],
                    'tagged_users': m.get('tagged_users', [])
                })
                if alt := m.get('alt'):
                    add_alt_text(alt, media_info['media_id'], session)
            # for convenience, so we can just pass list of strings
            elif isinstance(m, str):
                media_info = asyncio.run(upload_media(m, session))
                params['variables']['media']['media_entities'].append({
                    'media_id': media_info['media_id'],
                    'tagged_users': []
                })

    if reply_params := kwargs.get('reply_params', {}):
        params['variables'] |= reply_params
    if quote_params := kwargs.get('quote_params', {}):
        params['variables'] |= quote_params
    if poll_params := kwargs.get('poll_params', {}):
        params['variables'] |= poll_params

    url = f"https://api.twitter.com/graphql/{qid}/{operation}"
    r = session.post(url, headers=_get_headers(session), json=params)
    return r


def comment(text: str, tweet_id: int, session: Session, media: list[dict | str] = None):
    params = {"reply": {"in_reply_to_tweet_id": tweet_id, "exclude_reply_user_ids": []}}
    return create_tweet(text, session, media, reply_params=params)


def quote(text: str, screen_name: str, tweet_id: int, session: Session, media: list[dict | str] = None):
    """ no unquote operation, just DeleteTweet"""
    params = {"attachment_url": f"https://twitter.com/{screen_name}/status/{tweet_id}"}
    return create_tweet(text, session, media, quote_params=params)


def delete_tweet(tweet_id: int, session: Session):
    operation = Operation.DeleteTweet.name
    qid = operations[operation]['queryId']
    params = operations[operation]
    params['variables']['tweet_id'] = tweet_id
    url = f"https://api.twitter.com/graphql/{qid}/{operation}"
    r = session.post(url, headers=_get_headers(session), json=params)
    if 200 <= r.status_code < 300:
        logger.debug(f'{WARN}DELETE{RESET} tweet: {tweet_id}')
    return r.json()


def delete_all_tweets(user_id: int, session: Session):
    tweets = get_tweets(user_id, session)
    ids = set(map(int, find_key(find_key(tweets, 'tweet_results'), 'rest_id'))) - {user_id}
    [delete_tweet(_id, session) for _id in ids]


def retweet(tweet_id: int, session: Session):
    operation = Operation.CreateRetweet.name
    qid = operations[operation]['queryId']
    params = operations[operation]
    params['variables']['tweet_id'] = tweet_id
    url = f"https://api.twitter.com/graphql/{qid}/{operation}"
    r = session.post(url, headers=_get_headers(session), json=params)
    if 200 <= r.status_code < 300:
        logger.debug(f'{SUCCESS}RETWEET{RESET} tweet: {tweet_id}')
    return r.json()


def unretweet(tweet_id: int, session: Session):
    operation = Operation.DeleteRetweet.name
    qid = operations[operation]['queryId']
    params = operations[operation]
    params['variables']['source_tweet_id'] = tweet_id
    url = f"https://api.twitter.com/graphql/{qid}/{operation}"
    r = session.post(url, headers=_get_headers(session), json=params)
    if 200 <= r.status_code < 300:
        logger.debug(f'{SUCCESS}UNRETWEET{RESET} tweet: {tweet_id}')
    return r.json()


def get_tweets(user_id: int, session: Session):
    operation = Operation.UserTweets.name
    qid = operations[operation]['queryId']
    params = operations[operation]
    params['variables']['userId'] = user_id
    query = build_query(params)
    url = f"https://api.twitter.com/graphql/{qid}/{operation}?{query}"
    r = session.get(url, headers=_get_headers(session))
    return r.json()


@log(level=logging.DEBUG, info=['status_code', lambda r: r.json()['id']])
def follow(user_id: int, session: Session):
    settings = {
        "user_id": user_id,
        "include_profile_interstitial_type": "1",
        "include_blocking": "1",
        "include_blocked_by": "1",
        "include_followed_by": "1",
        "include_want_retweets": "1",
        "include_mute_edge": "1",
        "include_can_dm": "1",
        "include_can_media_tag": "1",
        "include_ext_has_nft_avatar": "1",
        "include_ext_is_blue_verified": "1",
        "include_ext_verified_type": "1",
        "skip_status": "1",
    }
    headers = _get_headers(session)
    headers['content-type'] = 'application/x-www-form-urlencoded'
    url = 'https://api.twitter.com/1.1/friendships/create.json'
    r = session.post(url, headers=headers, data=urlencode(settings))
    return r


@log(level=logging.DEBUG, info=['status_code', lambda r: r.json()['id']])
def unfollow(user_id: int, session: Session):
    _name = sys._getframe().f_code.co_name
    settings = {
        "user_id": user_id,
        "include_profile_interstitial_type": "1",
        "include_blocking": "1",
        "include_blocked_by": "1",
        "include_followed_by": "1",
        "include_want_retweets": "1",
        "include_mute_edge": "1",
        "include_can_dm": "1",
        "include_can_media_tag": "1",
        "include_ext_has_nft_avatar": "1",
        "include_ext_is_blue_verified": "1",
        "include_ext_verified_type": "1",
        "skip_status": "1",
    }
    headers = _get_headers(session)
    headers['content-type'] = 'application/x-www-form-urlencoded'
    url = 'https://api.twitter.com/1.1/friendships/destroy.json'
    r = session.post(url, headers=headers, data=urlencode(settings))
    return r


def mute(user_id: int, session: Session):
    _name = sys._getframe().f_code.co_name
    settings = {
        'user_id': user_id
    }
    try:
        headers = _get_headers(session)
        headers['content-type'] = 'application/x-www-form-urlencoded'
        url = 'https://api.twitter.com/1.1/mutes/users/create.json'
        r = session.post(url, headers=headers, data=urlencode(settings))
        if 200 <= r.status_code < 300:
            data = r.json()
            logger.debug(f'{SUCCESS}MUTE{RESET}: {data["id"]}')
            return data
    except Exception as e:
        logger.debug(f'[FAILED] {_name}: {e}')


def unmute(user_id: int, session: Session):
    _name = sys._getframe().f_code.co_name
    settings = {
        'user_id': user_id
    }
    try:
        headers = _get_headers(session)
        headers['content-type'] = 'application/x-www-form-urlencoded'
        url = 'https://api.twitter.com/1.1/mutes/users/destroy.json'
        r = session.post(url, headers=headers, data=urlencode(settings))
        if 200 <= r.status_code < 300:
            data = r.json()
            logger.debug(f'{WARN}UNMUTE{RESET}: {data["id"]}')
            return data
    except Exception as e:
        logger.debug(f'[FAILED] {_name}: {e}')


def enable_notifications(user_id: int, session: Session):
    _name = sys._getframe().f_code.co_name
    settings = {
        "id": user_id,
        "device": "true",
        "cursor": "-1",
        "include_profile_interstitial_type": "1",
        "include_blocking": "1",
        "include_blocked_by": "1",
        "include_followed_by": "1",
        "include_want_retweets": "1",
        "include_mute_edge": "1",
        "include_can_dm": "1",
        "include_can_media_tag": "1",
        "include_ext_has_nft_avatar": "1",
        "include_ext_is_blue_verified": "1",
        "include_ext_verified_type": "1",
        "skip_status": "1",
    }
    try:
        headers = _get_headers(session)
        headers['content-type'] = 'application/x-www-form-urlencoded'
        url = 'https://api.twitter.com/1.1/friendships/update.json'
        r = session.post(url, headers=headers, data=urlencode(settings))
        if 200 <= r.status_code < 300:
            data = r.json()
            twid = data["relationship"]["target"]["id"]
            logger.debug(f'{SUCCESS}ENABLE NOTIFICATIONS{RESET}: {twid}')
            return data
    except Exception as e:
        logger.debug(f'[FAILED] {_name}: {e}')


def disable_notifications(user_id: int, session: Session):
    _name = sys._getframe().f_code.co_name
    settings = {
        "id": user_id,
        "device": "false",
        "cursor": "-1",
        "include_profile_interstitial_type": "1",
        "include_blocking": "1",
        "include_blocked_by": "1",
        "include_followed_by": "1",
        "include_want_retweets": "1",
        "include_mute_edge": "1",
        "include_can_dm": "1",
        "include_can_media_tag": "1",
        "include_ext_has_nft_avatar": "1",
        "include_ext_is_blue_verified": "1",
        "include_ext_verified_type": "1",
        "skip_status": "1",
    }
    try:
        headers = _get_headers(session)
        headers['content-type'] = 'application/x-www-form-urlencoded'
        url = 'https://api.twitter.com/1.1/friendships/update.json'
        r = session.post(url, headers=headers, data=urlencode(settings))
        if 200 <= r.status_code < 300:
            data = r.json()
            twid = data["relationship"]["target"]["id"]
            logger.debug(f'{WARN}DISABLE NOTIFICATIONS{RESET}: {twid}')
            return data
    except Exception as e:
        logger.debug(f'[FAILED] {_name}: {e}')


def block(user_id: int, session: Session):
    _name = sys._getframe().f_code.co_name
    settings = {
        'user_id': user_id
    }
    try:
        headers = _get_headers(session)
        headers['content-type'] = 'application/x-www-form-urlencoded'
        url = 'https://api.twitter.com/1.1/blocks/create.json'
        r = session.post(url, headers=headers, data=urlencode(settings))
        if 200 <= r.status_code < 300:
            data = r.json()
            logger.debug(f'{SUCCESS}BLOCK{RESET}: {data["id"]}')
            return data
    except Exception as e:
        logger.debug(f'[FAILED] {_name}: {e}')


def unblock(user_id: int, session: Session):
    _name = sys._getframe().f_code.co_name
    settings = {
        'user_id': user_id
    }
    try:
        headers = _get_headers(session)
        headers['content-type'] = 'application/x-www-form-urlencoded'
        url = 'https://api.twitter.com/1.1/blocks/destroy.json'
        r = session.post(url, headers=headers, data=urlencode(settings))
        if 200 <= r.status_code < 300:
            data = r.json()
            logger.debug(f'{WARN}UNBLOCK{RESET}: {data["id"]}')
            return data
    except Exception as e:
        logger.debug(f'[FAILED] {_name}: {e}')


def update_search_settings(session: Session, **kwargs):
    _name = sys._getframe().f_code.co_name
    try:
        if kwargs.get('incognito'):
            kwargs.pop('incognito')
            settings = {
                "optInFiltering": False,
                "optInBlocking": True,
            }
        else:
            settings = {}
        settings |= kwargs
        twid = int(session.cookies.get_dict()['twid'].split('=')[-1].strip('"'))
        headers = _get_headers(session=session)
        r = session.post(
            url=f'https://api.twitter.com/1.1/strato/column/User/{twid}/search/searchSafety',
            headers=headers,
            json=settings,
        )
        if r.status_code == 200:
            logger.debug(f'[SUCCESS] {_name}: {settings}')
            return settings
    except Exception as e:
        logger.debug(f'[FAILED] {_name}: {e}')


def update_content_settings(session: Session, **kwargs):
    """
    Update content settings

    @param session: authenticated session
    @param kwargs: settings to enable/disable
    @return: updated settings
    """
    _name = sys._getframe().f_code.co_name
    try:
        if kwargs.get('incognito'):
            kwargs.pop('incognito')
            settings = {
                'include_mention_filter': True,
                'include_nsfw_user_flag': True,
                'include_nsfw_admin_flag': True,
                'include_ranked_timeline': True,
                'include_alt_text_compose': True,
                'display_sensitive_media': True,
                'protected': True,
                'discoverable_by_email': False,
                'discoverable_by_mobile_phone': False,
                'allow_dms_from': 'following',  ## {'all'}
                'dm_quality_filter': 'enabled',  ## {'disabled'}
                'dm_receipt_setting': 'all_disabled',  ## {'all_enabled'}
                'allow_media_tagging': 'none',  ## {'all', 'following'}
                'nsfw_user': False,
                'geo_enabled': False,  ## add location information to your tweets
                'allow_ads_personalization': False,
                'allow_logged_out_device_personalization': False,
                'allow_sharing_data_for_third_party_personalization': False,
                'allow_location_history_personalization': False,
            }
        else:
            settings = {}
        settings |= kwargs
        headers = _get_headers(session=session)
        headers['content-type'] = 'application/x-www-form-urlencoded'
        r = session.post(
            url='https://api.twitter.com/1.1/account/settings.json',
            headers=headers,
            data=urlencode(settings),  # case-insensitive, E.g. can be 'TRUE', True, 'true', etc.
        )
        if r.status_code == 200:
            logger.debug(f'[SUCCESS] {_name}: {settings}')
            return settings
    except Exception as e:
        logger.debug(f'[FAILED] {_name}: {e}')


def build_query(params):
    return '&'.join(f'{k}={ujson.dumps(v)}' for k, v in params.items())


def stats(rest_id: int, session: Session):
    """private endpoint?"""
    operation = Operation.TweetStats.name
    qid = operations[operation]['queryId']
    params = operations[operation]
    params['variables']['rest_id'] = rest_id
    query = build_query(params)
    url = f"https://api.twitter.com/graphql/{qid}/{operation}?{query}"
    r = session.get(url, headers=_get_headers(session))
    return r.json()

