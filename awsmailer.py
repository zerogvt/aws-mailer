#!/usr/bin/env python3
import smtplib
import os
import logging
import re
import sys
import time
import email.utils
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from os import listdir
from os.path import isfile, join

MAX_RECS_PER_BATCH = 50
# secsß
COOL_DOWN = 2
WAIT_ON_ERROR = 5
MAX_RETRIES = 5
# refresh smtp server connection after 10 mins
SERVER_TTL = 10 * 60.0
SMTP_SERVER = None
# persist notified users here - do not delete this file
NOTIFIED_FILE = 'notified.flatdb'
# implicit input - files with lists of who is to be notified should live in this dir
RECIPIENTS_DIR = 'contacts'
# what is to be mailed - see example_message.txt for an example/layout
MSG_FILE = 'message.txt'

LAST_SERVER_TIMESTAMP = time.time()
# This address must be verified in AWS SES
SENDER = os.environ['SENDER_SMTP']
SENDERNAME = os.environ['SENDERNAME_SMTP']
# Replace smtp_username with your Amazon SES SMTP user name.
USERNAME_SMTP = os.environ['USERNAME_SMTP']
# Replace smtp_password with your Amazon SES SMTP password.
PASSWORD_SMTP = os.environ['PASSWORD_SMTP']
# (Optional) the name of a configuration set to use for this message.
# If you comment out this line, you also need to remove or comment out
# the "X-SES-CONFIGURATION-SET:" header below.
#CONFIGURATION_SET = "ConfigSet"

# If you're using Amazon SES in an AWS Region other than US West (Oregon),
# replace email-smtp.us-west-2.amazonaws.com with the Amazon SES SMTP
# endpoint in the appropriate region.
HOST = "email-smtp.us-east-1.amazonaws.com"
PORT = 587

class SMTP_Server:
    class __SMTP_Server:
        def __init__(self):
            self.val = arg
        def __str__(self):
            return repr(self) + self.val
    instance = None
    def __init__(self, arg):
        if not OnlyOne.instance:
            OnlyOne.instance = OnlyOne.__OnlyOne(arg)
        else:
            OnlyOne.instance.val = arg
    def __getattr__(self, name):
        return getattr(self.instance, name)


def get_a_logger():
    """
    Returns a logger
    """
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    fh = logging.FileHandler('awsmailer.log')
    fh.setLevel(logging.DEBUG)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


def is_valid_email(email):
    """
    Simple regex email addr checker
    """
    if len(email) > 5:
        if re.match('^[_a-z0-9-]+(\.[_a-z0-9-]+)*@[a-z0-9-]+(\.[a-z0-9-]+)*(\.[a-z]{2,20})$', email) != None:
            return True
    return False


def read_parse_msg(path):
    """
    A simple parser. See example_message.txt for the proper message format.
    """
    keys = ['__SUBJECT', '__BODY_TEXT', '__BODY_HTML']
    msg = {}
    for k in keys:
        msg[k] = []

    key = None
    count = 0
    with open(path) as fh:
        for ln in fh:
            line = ln.strip()
            count += 1
            # found a key - beggining or ending of a section
            if line in keys:
                # if no current key this is a beginning
                if key == None:
                    key = line
                # if current key same as the one we found this is an ending
                elif key in line:
                    key = None
                # else input is malformed - abort
                else:
                    logger.error('Malformed message on line: %s' % count)
            # normal input
            else:
                if key is not None:
                    msg[key].append(line)
    return( "".join(msg['__SUBJECT']),
            "".join(msg['__BODY_TEXT']),
            "".join(msg['__BODY_HTML']) )


def read_already_notified(notified_fpath):
    """
    Reads list of already notified addresses and adds them in a dictionary.
    """
    notified = {}
    if os.path.isfile(NOTIFIED_FILE):
        logger.info('Loading already notified recipients.')
        count = 0
        with open(NOTIFIED_FILE) as fh:
            for line in fh:
                count += 1
                notified[line.strip().lower()] = 1
        logger.info('Loaded %s notified recipients.' % count)
    return notified


def read_recipients_lists(dirpath):
    """
    Alls files inside the 'contacts' directory are read through.
    The files should contain lists of emails addresses separated by new lines.
    """
    recipients_list = []
    files = [f for f in listdir(dirpath) if isfile(join(dirpath, f))]
    for f in files:
        with open(join(dirpath, f)) as fh:
            for line in fh:
                recipients_list.append(line.strip())
    logger.info('Loaded %s recipients from %s lists' % (len(recipients_list),
                                                        len(files)))
    return recipients_list


def refresh_smtp_server():
    """
    Gets or refreshes a connection to an AWS SES SMTP server
    """
    global LAST_SERVER_TIMESTAMP
    global SMTP_SERVER
    try:
        SMTP_SERVER = smtplib.SMTP(HOST, PORT)
        SMTP_SERVER.ehlo()
        SMTP_SERVER.starttls()
        #stmplib docs recommend calling ehlo() before & after starttls()
        SMTP_SERVER.ehlo()
        SMTP_SERVER.login(USERNAME_SMTP, PASSWORD_SMTP)
        logger.info("Logged in SMTP server %s" % HOST)
    except Exception as e:
        logger.error(e)
        SMTP_SERVER.close()
        sys.exit(2)
    LAST_SERVER_TIMESTAMP = time.time()
    logger.info('New smtp server connection created. TS: %s' % LAST_SERVER_TIMESTAMP)


def create_smtp_msg(subject, sendername, sender, recipient_list,
                    body_text, body_html):
    """
    Creates an smtp message
    """
    # Create message container - the correct MIME type is multipart/alternative.
    msg = MIMEMultipart('alternative')
    msg['Subject'] = subject
    msg['From'] = email.utils.formataddr((sendername, sender))
    msg['Bcc'] = ','.join(recipient_list)
    # Comment or delete the next line if you are not using a configuration set
    #msg.add_header('X-SES-CONFIGURATION-SET',CONFIGURATION_SET)
    # Record the MIME types of both parts - text/plain and text/html.
    part1 = MIMEText(body_text, 'plain')
    part2 = MIMEText(body_html, 'html')
    # Attach parts into message container.
    # According to RFC 2046, the last part of a multipart message, in this case
    # the HTML message, is best and preferred.
    msg.attach(part1)
    msg.attach(part2)
    return msg


def batch_send(sender, recipients_batch, msg, notified):
    """
    Sends an smtp msg as a blind copy (bcc) to a list of recipients
    """
    if len(recipients_batch) == 0:
        logger.info("No recipients. Exiting")
        sys.exit(0)
    if not SMTP_SERVER:
        logger.info("No SMTP server connection. Getting one...")
        refresh_smtp_server()
    if time.time() - LAST_SERVER_TIMESTAMP > SERVER_TTL:
        logger.info("SMTP Server connection exceeded TTL (%s). Getting a new one..." % (time.time() - LAST_SERVER_TIMESTAMP))
        refresh_smtp_server()
    mail_count = 0
    retries = 0
    while True:
        try:
            SMTP_SERVER.sendmail(sender, recipients_batch, msg.as_string())
            for recipient in recipients_batch:
                logger.info("%s done" % recipient)
                with open(NOTIFIED_FILE, "a") as fh:
                    fh.write("%s\n" % recipient)
                    notified[recipient] = 1
                    mail_count += 1
        except Exception as e:
            logger.error(e)
            retries += 1
            time.sleep(retries * WAIT_ON_ERROR)
            logger.info('Retry %s' % retries)
            refresh_smtp_server()
            if retries >= MAX_RETRIES:
                logger.error('Max retries reached. Aborting.')
                SMTP_SERVER.close()
                sys.exit(1)
            continue
        # break out of while if no exception
        break
    return mail_count


def should_skip(recipient, notified):
    """
    Should recipient be skipped?
    """
    if not is_valid_email(recipient):
        logger.info('Ignoring %s (malformed email)' % recipient)
        return True
    if recipient in notified:
        logger.info('Ignoring %s (already in notified list)' % recipient)
        return True
    return False


logger = get_a_logger()
logger.info('>> Sending batch emails with message:')
(subject, body_txt, body_html) = read_parse_msg(MSG_FILE)
logger.info('Message Subject: %s' % subject)
logger.info('Message Text: %s' % body_txt)
logger.info('Message HTML: %s' % body_html)
notified = read_already_notified(NOTIFIED_FILE)
all_recipients_list = read_recipients_lists(RECIPIENTS_DIR)

mail_count = 0
total_count = 0
recipients_batch = []
start_time = time.time()
for rec in all_recipients_list:
    total_count += 1
    recipient = rec.strip().lower()
    if not should_skip(recipient, notified):
        recipients_batch.append(recipient)
    if len(recipients_batch) == MAX_RECS_PER_BATCH or total_count == len(all_recipients_list):
        msg = create_smtp_msg(subject, SENDERNAME, SENDER, recipients_batch,
                              body_txt, body_html)
        mail_count += batch_send(SENDER, recipients_batch, msg, notified)
        recipients_batch.clear()
        elapsed_time = time.time() - start_time
        logger.info('Sent %s mails in %s mins' % (mail_count, round(elapsed_time/60,2)))
        # Throttle down
        time.sleep(COOL_DOWN)
SMTP_SERVER.close()