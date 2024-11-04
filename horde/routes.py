# SPDX-FileCopyrightText: 2022 Konstantinos Thoukydidis <mail@dbzer0.com>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import random
import secrets
from uuid import uuid4

import oauthlib
import requests
from flask import redirect, render_template, request, send_from_directory, url_for
from flask_dance.contrib.discord import discord
from flask_dance.contrib.github import github
from flask_dance.contrib.google import google
from markdown import markdown

from horde import vars as hv
from horde.argparser import maintenance
from horde.classes.base import settings
from horde.classes.base.news import News
from horde.classes.base.user import User
from horde.consts import HORDE_API_VERSION, HORDE_VERSION
from horde.countermeasures import CounterMeasures
from horde.database import functions as database
from horde.flask import HORDE, cache, db
from horde.logger import logger
from horde.patreon import patrons
from horde.utils import ConvertAmount, hash_api_key, is_profane, sanitize_string
from horde.vars import (
    google_verification_string,
    horde_contact_email,
    horde_logo,
    horde_repository,
    horde_title,
    horde_url,
    img_url,
)

dance_return_to = "/"


@logger.catch(reraise=True)
@HORDE.route("/")
# @cache.cached(timeout=300)
def index():
    with open(os.getenv("HORDE_MARKDOWN_INDEX", "index_stable.md")) as index_file:
        index = index_file.read()
    align_image = 0
    big_image = align_image
    while big_image == align_image:
        big_image = random.randint(1, 5)
    policies = """
## Policies

[Privacy Policy](/privacy)

[Terms of Service](/terms)"""
    news = ""
    sorted_news = News().sorted_news()
    for riter in range(len(sorted_news)):
        news += f"* {sorted_news[riter]['newspiece']}\n"
        if riter > 1:
            break
    totals = database.get_total_usage()
    processing_totals = database.retrieve_totals()
    (
        interrogation_worker_count,
        interrogation_worker_thread_count,
    ) = database.count_active_workers("interrogation")
    image_worker_count, image_worker_thread_count = database.count_active_workers("image")
    text_worker_count, text_worker_thread_count = database.count_active_workers("text")
    avg_performance = ConvertAmount(database.get_request_avg() * image_worker_thread_count)
    avg_text_performance = ConvertAmount(database.get_request_avg("text") * image_worker_thread_count)
    # We multiple with the divisor again, to get the raw amount, which we can convert to prefix accurately
    total_image_things = ConvertAmount(totals[hv.thing_names["image"]] * hv.thing_divisors["image"])
    total_text_things = ConvertAmount(totals[hv.thing_names["text"]] * hv.thing_divisors["text"])
    queued_image_things = ConvertAmount(
        processing_totals[f"queued_{hv.thing_names['image']}"] * hv.thing_divisors["image"],
    )
    queued_text_things = ConvertAmount(
        processing_totals[f"queued_{hv.thing_names['text']}"] * hv.thing_divisors["text"],
    )
    total_image_fulfillments = ConvertAmount(totals["image_fulfilments"])
    total_text_fulfillments = ConvertAmount(totals["text_fulfilments"])
    total_forms = ConvertAmount(totals["forms"])
    findex = index.format(
        page_title=horde_title,
        horde_img_url=img_url,
        horde_image=align_image,
        avg_performance=avg_performance.amount,
        avg_thing_name=avg_performance.prefix + hv.raw_thing_names["image"],
        avg_text_performance=avg_text_performance.amount,
        avg_text_thing_name=avg_text_performance.prefix + hv.raw_thing_names["text"],
        total_image_things=total_image_things.amount,
        total_total_image_things_name=total_image_things.prefix + hv.raw_thing_names["image"],
        total_text_things=total_text_things.amount,
        total_text_things_name=total_text_things.prefix + hv.raw_thing_names["text"],
        total_image_fulfillments=total_image_fulfillments.amount,
        total_image_fulfillments_char=total_image_fulfillments.char,
        total_text_fulfillments=total_text_fulfillments.amount,
        total_text_fulfillments_char=total_text_fulfillments.char,
        total_forms=total_forms.amount,
        total_forms_char=total_forms.char,
        image_workers=image_worker_count,
        image_worker_threads=image_worker_thread_count,
        text_workers=text_worker_count,
        text_worker_threads=text_worker_thread_count,
        interrogation_workers=interrogation_worker_count,
        interrogation_worker_threads=interrogation_worker_thread_count,
        total_image_queue=processing_totals["queued_requests"],
        total_text_queue=processing_totals["queued_text_requests"],
        total_forms_queue=processing_totals.get("queued_forms", 0),
        queued_image_things=queued_image_things.amount,
        queued_image_things_name=queued_image_things.prefix + hv.raw_thing_names["image"],
        queued_text_things=queued_text_things.amount,
        queued_text_things_name=queued_text_things.prefix + hv.raw_thing_names["text"],
        maintenance_mode=maintenance.active,
        news=news,
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
    <title>{horde_title}</title>
    <meta name="google-site-verification" content="{google_verification_string}" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    {style}
    </head>
    """
    return head + markdown(findex + policies)


@HORDE.route("/sponsors")
@logger.catch(reraise=True)
@cache.cached(timeout=300)
def patrons_route():
    all_patrons = ", ".join(patrons.get_names(min_entitlement=3, max_entitlement=99))
    return render_template(
        "document.html",
        doc="sponsors.html",
        page_title="Sponsors",
        all_patrons=all_patrons,
        all_sponsors=patrons.get_sponsors(),
    )


@logger.catch(reraise=True)
def get_oauth_id():
    google_data = None
    discord_data = None
    github_data = None
    patreon_data = None
    authorized = False
    if google.authorized:
        google_user_info_endpoint = "/oauth2/v2/userinfo"
        try:
            google_data = google.get(google_user_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    if not authorized and discord.authorized:
        discord_info_endpoint = "/api/users/@me"
        try:
            discord_data = discord.get(discord_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    if not authorized and github.authorized:
        github_info_endpoint = "/user"
        try:
            github_data = github.get(github_info_endpoint).json()
            authorized = True
        except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
            pass
    # if not authorized and patreon.OAuth(os.getenv("PATREON_CLIENT_ID"), os.getenv("PATREON_CLIENT_SECRET")):
    #     patreon_info_endpoint = '/api/oauth2/token'
    #     try:
    #         patreon_data = github.get(patreon_info_endpoint).json()
    #         authorized = True
    #     except oauthlib.oauth2.rfc6749.errors.TokenExpiredError:
    #         pass
    oauth_id = None
    if google_data:
        oauth_id = f'g_{google_data["id"]}'
    elif discord_data:
        oauth_id = f'd_{discord_data["id"]}'
    elif github_data:
        oauth_id = f'gh_{github_data["id"]}'
    elif patreon_data:
        oauth_id = f'p_{patreon_data["id"]}'
    return oauth_id


@logger.catch(reraise=True)
@HORDE.route("/register", methods=["GET", "POST"])
def register():
    api_key = None
    user = None
    welcome = "Welcome"
    username = ""
    pseudonymous = False
    oauth_id = get_oauth_id()
    if oauth_id:
        user = database.find_user_by_oauth_id(oauth_id)
        if user:
            username = user.username
    use_recaptcha = True
    secret_key = os.getenv("RECAPTCHA_SECRET_KEY")
    if not secret_key:
        use_recaptcha = False
    if request.method == "POST":
        if use_recaptcha:
            try:
                recaptcha_response = request.form["g-recaptcha-response"]
                payload = {"response": recaptcha_response, "secret": secret_key}
                response = requests.post("https://www.google.com/recaptcha/api/siteverify", payload)
                if not response.ok or not response.json()["success"]:
                    return render_template(
                        "recaptcha_error.html",
                        page_title="Recaptcha validation Error!",
                        use_recaptcha=False,
                    )
                ip_timeout = CounterMeasures.retrieve_timeout(request.remote_addr)
                if ip_timeout:
                    return render_template(
                        "ipaddr_ban_error.html",
                        page_title="IP Address Banned",
                        use_recaptcha=False,
                    )
            except Exception as err:
                logger.error(err)
                return render_template(
                    "recaptcha_error.html",
                    page_title="Recaptcha Submit Error!",
                    use_recaptcha=False,
                )
        api_key = secrets.token_urlsafe(16)
        hashed_api_key = hash_api_key(api_key)
        if user:
            username = sanitize_string(request.form["username"])
            if is_profane(username):
                return render_template("bad_username.html", page_title="Bad Username")
            user.username = username
            user.api_key = hashed_api_key
            db.session.commit()
        else:
            # Triggered when the user created a username without logging in
            if is_profane(request.form["username"]):
                return render_template("bad_username.html", page_title="Bad Username")
            if not oauth_id:
                oauth_id = str(uuid4())
                pseudonymous = True
                if settings.mode_raid():
                    return render_template(
                        "error.html",
                        page_title="Not Allowed",
                        error_message="We cannot allow anonymous registrations at the moment. "
                        "Please use one of the oauth2 buttons to login first.",
                    )
            username = sanitize_string(request.form["username"])
            user = User(username=username, oauth_id=oauth_id, api_key=hashed_api_key)
            user.create()
    if user:
        welcome = f"Welcome back {user.get_unique_alias()}"
    return render_template(
        "register.html",
        page_title=f"Join the {horde_title}!",
        use_recaptcha=use_recaptcha,
        recaptcha_site=os.getenv("RECAPTCHA_SITE_KEY"),
        welcome=welcome,
        user=user,
        api_key=api_key,
        username=username,
        pseudonymous=pseudonymous,
        oauth_id=oauth_id,
    )


@logger.catch(reraise=True)
@HORDE.route("/transfer", methods=["GET", "POST"])
def transfer():
    src_user = None
    dest_username = None
    kudos = None
    error = None
    welcome = "Welcome"
    oauth_id = get_oauth_id()
    if oauth_id:
        src_user = database.find_user_by_oauth_id(oauth_id)
        if not src_user:
            # This probably means the user was deleted
            oauth_id = None
    if request.method == "POST":
        dest_username = request.form["username"]
        amount = request.form["amount"]
        if not amount.isnumeric():
            kudos = 0
            error = "Please enter a number in the kudos field"
        # Triggered when the user submited without logging in
        elif src_user:
            ret = database.transfer_kudos_to_username(src_user, dest_username, int(amount))
            kudos = ret[0]
            error = ret[1]
        else:
            ret = database.transfer_kudos_from_apikey_to_username(
                request.form["src_api_key"],
                dest_username,
                int(amount),
            )
            kudos = ret[0]
            error = ret[1]
    if src_user:
        welcome = f"Welcome back {src_user.get_unique_alias()}. You have {src_user.kudos} kudos remaining"
    return render_template(
        "transfer_kudos.html",
        page_title="Kudos Transfer",
        welcome=welcome,
        kudos=kudos,
        error=error,
        dest_username=dest_username,
        oauth_id=oauth_id,
    )


@HORDE.route("/google/<return_to>")
def google_login(return_to):
    global dance_return_to
    dance_return_to = "/" + return_to
    return redirect(url_for("google.login"))


@HORDE.route("/discord/<return_to>")
def discord_login(return_to):
    global dance_return_to
    dance_return_to = "/" + return_to
    return redirect(url_for("discord.login"))


@HORDE.route("/github/<return_to>")
def github_login(return_to):
    global dance_return_to
    dance_return_to = "/" + return_to
    return redirect(url_for("github.login"))


# @HORDE.route('/patreon/<return_to>')
# def patreon_login(return_to):
#     global dance_return_to
#     dance_return_to = '/' + return_to
#     return redirect('/patreon/patreon')


@HORDE.route("/finish_dance")
def finish_dance():
    global dance_return_to
    redirect_url = dance_return_to
    dance_return_to = "/"
    return redirect(redirect_url)


@HORDE.route("/privacy")
def privacy():
    return render_template(
        "document.html",
        doc=os.getenv("HORDE_HTML_TERMS", "privacy_policy.html"),
        horde_title=horde_title,
        horde_url=horde_url,
        horde_contact_email=horde_contact_email,
    )


@HORDE.route("/terms")
def terms():
    return render_template(
        "document.html",
        doc=os.getenv("HORDE_HTML_TERMS", "terms_of_service.html"),
        horde_title=horde_title,
        horde_url=horde_url,
        horde_contact_email=horde_contact_email,
    )


@HORDE.route("/assets/<filename>")
def assets(filename):
    return send_from_directory("../assets", filename)


@HORDE.route("/.well-known/serviceinfo")
def serviceinfo():
    return {
        "version": "0.2",
        "software": {
            "name": horde_title,
            "version": HORDE_VERSION,
            "repository": horde_repository,
            "homepage": horde_url,
            "logo": horde_logo,
        },
        "api": {
            "aihorde": {
                "name": "AI Horde API",
                "version": HORDE_API_VERSION,
                "base_url": f"{horde_url}/api/v2",
                "rel_url": "/api/v2",
                "documentation": f"{horde_url}/api",
            },
        },
    }, 200
