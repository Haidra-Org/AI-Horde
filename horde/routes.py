import oauthlib
import random
import secrets
from uuid import uuid4

from flask import render_template, redirect, url_for, request
from flask_dance.contrib.discord import discord
from flask_dance.contrib.github import github
from flask_dance.contrib.google import google
from markdown import markdown

from horde.database import functions as database
from horde.argparser import args, maintenance
from horde.classes import News, User, stats
from horde.flask import HORDE, cache
from horde.logger import logger
from horde.utils import ConvertAmount, is_profane, sanitize_string, hash_api_key
from .vars import thing_name, raw_thing_name, thing_divisor, google_verification_string, img_url, horde_title

dance_return_to = '/'

@logger.catch(reraise=True)
@HORDE.route('/')
@cache.cached()
def index():
    with open(f'index_{args.horde}.md') as index_file:
        index = index_file.read()
    top_contributor = database.get_top_contributor()
    top_worker = database.get_top_worker()
    align_image = 0
    big_image = align_image
    while big_image == align_image:
        big_image = random.randint(1, 5)
    if not top_contributor or not top_worker:
        top_contributors = f'\n<img src="{img_url}/{big_image}.jpg" width="800" />'
    else:
        # We don't use the prefix char, so we just discard it
        top_contrib_things = ConvertAmount(top_contributor.contributed_thing * thing_divisor)
        top_contrib_fulfillments = ConvertAmount(top_contributor.contributed_fulfillments)
        top_worker_things = ConvertAmount(top_worker.contributions * thing_divisor)
        top_worker_fulfillments = ConvertAmount(top_worker.fulfilments)
        top_contributors = f"""\n## Top Contributors
These are the people and workers who have contributed most to this horde.
### Users
This is the person whose worker(s) have generated the most pixels for the horde.
#### {top_contributor.get_unique_alias()}
* {top_contrib_things.amount} {top_contrib_things.prefix + raw_thing_name} generated.
* {top_contrib_fulfillments.amount}{top_contrib_fulfillments.char} requests fulfilled.
### Workers
This is the worker which has generated the most pixels for the horde.
#### {top_worker.name}
* {top_worker_things.amount} {top_worker_things.prefix + raw_thing_name} generated.
* {top_worker_fulfillments.amount}{top_worker_fulfillments.char} request fulfillments.
* {top_worker.get_human_readable_uptime()} uptime.

<img src="{img_url}/{big_image}.jpg" width="800" />
"""
    policies = """
## Policies

[Privacy Policy](/privacy)

[Terms of Service](/terms)"""
    news = ""
    sorted_news =News().sorted_news()
    for iter in range(len(sorted_news)):
        news += f"* {sorted_news[iter]['newspiece']}\n"
        if iter > 1: break
    totals = database.get_total_usage()
    wp_totals = database.count_totals()
    active_worker_count = database.count_active_workers()
    avg_performance = ConvertAmount(
        stats.get_request_avg(database.get_worker_performances())
        * active_worker_count
    )
    # We multiple with the divisor again, to get the raw amount, which we can conver to prefix accurately
    total_things = ConvertAmount(totals[thing_name] * thing_divisor)
    queued_things = ConvertAmount(wp_totals[f"queued_{thing_name}"] * thing_divisor)
    total_fulfillments = ConvertAmount(totals["fulfilments"])
    findex = index.format(
        horde_title = horde_title,
        horde_img_url = img_url,
        horde_image = align_image,
        avg_performance= avg_performance.amount,
        avg_thing_name= avg_performance.prefix + raw_thing_name,
        total_things = total_things.amount,
        total_things_name = total_things.prefix + raw_thing_name,
        total_fulfillments = total_fulfillments.amount,
        total_fulfillments_char = total_fulfillments.char,
        active_workers = active_worker_count,
        total_queue = wp_totals["queued_requests"],
        queued_things = queued_things.amount,
        queued_things_name = queued_things.prefix + raw_thing_name,
        maintenance_mode = maintenance.active,
        news = news,
    )

    style = """<style>
        body {
            max-width: 120ex;
            margin: 0 auto;
            color: #333333;
            line-height: 1.4;
            font-family: sans-serif;
            padding: 1em;
        }
        </style>
    """
    
    head = f"""<head>
    <title>{horde_title} Horde</title>
    <meta name="google-site-verification" content="{google_verification_string}" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    {style}
    </head>
    """
    return(head + markdown(findex + top_contributors + policies))


@logger.catch(reraise=True)
def get_oauth_id():
    google_data = None
    discord_data = None
    github_data = None
    authorized = False
    if google.authorized:
        google_user_info_endpoint = '/oauth2/v2/userinfo'
        try:
            google_data = google.get(google_user_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    if not authorized and discord.authorized:
        discord_info_endpoint = '/api/users/@me'
        try:
            discord_data = discord.get(discord_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    if not authorized and github.authorized:
        github_info_endpoint = '/user'
        try:
            github_data = github.get(github_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    oauth_id = None
    if google_data:
        oauth_id = f'g_{google_data["id"]}'
    elif discord_data:
        oauth_id = f'd_{discord_data["id"]}'
    elif github_data:
        oauth_id = f'gh_{github_data["id"]}'
    return(oauth_id)


@logger.catch(reraise=True)
@HORDE.route('/register', methods=['GET', 'POST'])
def register():
    api_key = None
    user = None
    welcome = 'Welcome'
    username = ''
    pseudonymous = False
    oauth_id = get_oauth_id()
    if oauth_id:
        user = database.find_user_by_oauth_id(oauth_id)
        if user:
            username = user.username
    if request.method == 'POST':
        api_key = secrets.token_urlsafe(16)
        hashed_api_key = hash_api_key(api_key)
        if user:
            username = sanitize_string(request.form['username'])
            if is_profane(username):
                return render_template('bad_username.html', page_title="Bad Username")
            user.username = username
            user.api_key = hashed_api_key
        else:
            # Triggered when the user created a username without logging in
            if is_profane(request.form['username']):
                return render_template('bad_username.html', page_title="Bad Username")
            if not oauth_id:
                oauth_id = str(uuid4())
                pseudonymous = True
            username = sanitize_string(request.form['username'])
            user = User(
                username=username,
                oauth_id=oauth_id,
                api_key=hashed_api_key)
            user.create()
    if user:
        welcome = f"Welcome back {user.get_unique_alias()}"
    return render_template('register.html',
                           page_title="Join the Stable Horde!",
                           welcome=welcome,
                           user=user,
                           api_key=api_key,
                           username=username,
                           pseudonymous=pseudonymous,
                           oauth_id=oauth_id)


@logger.catch(reraise=True)
@HORDE.route('/transfer', methods=['GET', 'POST'])
def transfer():
    src_api_key = None
    src_user = None
    dest_username = None
    kudos = None
    error = None
    welcome = 'Welcome'
    oauth_id = get_oauth_id()
    if oauth_id:
        src_user = database.find_user_by_oauth_id(oauth_id)
        if not src_user:
            # This probably means the user was deleted
            oauth_id = None
    if request.method == 'POST':
        dest_username = request.form['username']
        amount = request.form['amount']
        if not amount.isnumeric():
            kudos = 0
            error = "Please enter a number in the kudos field"
        # Triggered when the user submited without logging in
        elif src_user:
            ret = database.transfer_kudos_to_username(src_user,dest_username,int(amount))
            kudos = ret[0]
            error = ret[1]
        else:
            ret = database.transfer_kudos_from_apikey_to_username(request.form['src_api_key'],dest_username,int(amount))
            kudos = ret[0]
            error = ret[1]
    if src_user:
        welcome = f"Welcome back {src_user.get_unique_alias()}. You have {src_user.kudos} kudos remaining"
    return render_template('transfer_kudos.html',
                           page_title="Kudos Transfer",
                           welcome=welcome,
                           kudos=kudos,
                           error=error,
                           dest_username=dest_username,
                           oauth_id=oauth_id)


@HORDE.route('/google/<return_to>')
def google_login(return_to):
    global dance_return_to
    dance_return_to = '/' + return_to
    return redirect(url_for('google.login'))


@HORDE.route('/discord/<return_to>')
def discord_login(return_to):
    global dance_return_to
    dance_return_to = '/' + return_to
    return redirect(url_for('discord.login'))


@HORDE.route('/github/<return_to>')
def github_login(return_to):
    global dance_return_to
    dance_return_to = '/' + return_to
    return redirect(url_for('github.login'))


@HORDE.route('/finish_dance')
def finish_dance():
    global dance_return_to
    redirect_url = dance_return_to
    dance_return_to = '/'
    return redirect(redirect_url)


@HORDE.route('/privacy')
def privacy():
    return render_template('privacy_policy.html')

@HORDE.route('/terms')
def terms():
    return render_template('terms_of_service.html')
