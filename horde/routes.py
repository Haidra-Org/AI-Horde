from flask import render_template, redirect, url_for, request
import random, time, os, oauthlib, secrets, logging
from flask_dance.contrib.google import google
from flask_dance.contrib.discord import discord
from flask_dance.contrib.github import github
from markdown import markdown
from uuid import uuid4
from . import logger, maintenance, HORDE
from .classes import db
from .classes import waiting_prompts,User
import bleach

dance_return_to = '/'

@logger.catch
@HORDE.route('/')
def index():
    with open('index.md') as index_file:
        index = index_file.read()
    top_contributor = db.get_top_contributor()
    top_worker = db.get_top_worker()
    align_image = 0
    big_image = align_image
    while big_image == align_image:
        big_image = random.randint(1, 5)
    if not top_contributor or not top_worker:
        top_contributors = f'\n<img src="https://github.com/db0/Stable-Horde/blob/master/img/{big_image}.png?raw=true" width="800" />'
    else:
        top_contributors = f"""\n## Top Contributors
These are the people and workers who have contributed most to this horde.
### Users
This is the person whose worker(s) have generated the most pixels for the horde.
#### {top_contributor.get_unique_alias()}
* {round(top_contributor.contributions['megapixelsteps'] / 1000,2)} Gigapixelsteps generated.
* {top_contributor.contributions['fulfillments']} requests fulfilled.
### Workers
This is the worker which has generated the most pixels for the horde.
#### {top_worker.name}
* {round(top_worker.contributions/1000,2)} Gigapixelsteps generated.
* {top_worker.fulfilments} request fulfillments.
* {top_worker.get_human_readable_uptime()} uptime.
"""
    policies = """
## Policies

[Privacy Policy](/privacy)

[Terms of Service](/terms)"""
    totals = db.get_total_usage()
    wp_totals = waiting_prompts.count_totals()
    findex = index.format(
        stable_image = align_image,
        avg_performance= round(db.stats.get_request_avg() / 1000000,2),
        total_pixels = round(totals["megapixelsteps"] / 1000,2),
        total_fulfillments = totals["fulfilments"],
        active_workers = db.count_active_workers(),
        total_queue = wp_totals["queued_requests"],
        total_mps = wp_totals["queued_megapixelsteps"],
        maintenance_mode = maintenance.active,
    )
    head = """<head>
    <title>Stable Horde</title>
    <meta name="google-site-verification" content="pmLKyCEPKM5csKT9mW1ZbGLu2TX_wD0S5FCxWlmg_iI" />
    </head>
    """
    return(head + markdown(findex + top_contributors + policies))


@logger.catch
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


@logger.catch
@HORDE.route('/register', methods=['GET', 'POST'])
def register():
    api_key = None
    user = None
    welcome = 'Welcome'
    username = ''
    pseudonymous = False
    oauth_id = get_oauth_id()
    if oauth_id:
        user = db.find_user_by_oauth_id(oauth_id)
        if user:
            username = user.username
    if request.method == 'POST':
        api_key = secrets.token_urlsafe(16)
        if user:
            username = bleach.clean(request.form['username'])
            user.username = username
            user.api_key = api_key
        else:
            # Triggered when the user created a username without logging in
            if not oauth_id:
                oauth_id = str(uuid4())
                pseudonymous = True
            user = User(db)
            user.create(request.form['username'], oauth_id, api_key, None)
            username = bleach.clean(request.form['username'])
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


@logger.catch
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
        src_user = db.find_user_by_oauth_id(oauth_id)
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
            ret = db.transfer_kudos_to_username(src_user,dest_username,int(amount))
            kudos = ret[0]
            error = ret[1]
        else:
            ret = db.transfer_kudos_from_apikey_to_username(request.form['src_api_key'],dest_username,int(amount))
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