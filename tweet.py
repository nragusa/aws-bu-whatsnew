import boto3
import html
import json
import os
import re
import sys
import urllib

from botocore.exceptions import ClientError
from botocore.vendored import requests
from boto3.dynamodb.conditions import Key

HERE = os.path.dirname(os.path.realpath(__file__))
DIST_PKGS = os.path.join(HERE, 'dist-packages')
sys.path.append(DIST_PKGS)

import feedparser
import tweepy

FEED_URL = os.environ.get('feedurl', None)
DYNAMODB_TABLE = os.environ['dynamodb_table']
BU_HASHTAG = os.environ['bu_hashtag']
MY_HASHTAG = os.environ.get('my_hashtag', 'MyFirstNameLovesECS')

def shorten_url(url):
    # Get bitly login and API key from secrets manager
    BITLY_LOGIN = os.environ['bitly_login']
    BITLY_API_KEY = os.environ['bitly_api_key']
    if(os.environ.get('BITLY_LOGIN_VALUE', None) and
        os.environ.get('BITLY_API_KEY_VALUE', None)):
        # Values are cached - used those
        bitly_login = os.environ['BITLY_LOGIN_VALUE']
        bitly_api_key = os.environ['BITLY_API_KEY_VALUE']
    else:
        try:
            ssm = boto3.client('ssm')
            bitly_login = ssm.get_parameter(
                Name=BITLY_LOGIN,
                WithDecryption=True
            )['Parameter']['Value']
            api_key = ssm.get_parameter(
                Name=BITLY_API_KEY,
                WithDecryption=True
            )['Parameter']['Value']
        except ClientError as error:
            print('Problem getting bitly secrets: {}'.format(error))
            return ''
        except KeyError as error:
            print('Value not returned from Parameter Store API')
            return ''
    
    # Now that we have the secrets, call the bitly service
    try:
        params = urllib.parse.urlencode({
            'login': bitly_login,
            'apiKey': api_key,
            'longUrl': url
        })
        api_url = 'https://api-ssl.bitly.com/v3/shorten?%s' % params
        response = requests.get(api_url)
        if response.status_code == 200:
            data = json.loads(response.text)['data']
    except requests.exceptions.HTTPError as error:
        print('Problem calling bitly API: {}'.format(error))
    except KeyError as error:
        print('Unexpected data returned from bitly API: {}'.format(error))
    else:
        return data['url']
    return ''

def tweet(title, short_url):
    # Get the name of the parameters stored in SSM from the environment variables
    CONSUMER_KEY = os.environ['consumer_key']
    CONSUMER_SECRET = os.environ['consumer_secret']
    ACCESS_TOKEN = os.environ['access_token']
    ACCESS_SECRET = os.environ['access_secret']
    
    tweet = '{} #{} #{} {}'.format(title, BU_HASHTAG, MY_HASHTAG, short_url)
    if len(tweet) > 280:
        # Shorten title by the length of the mandatory text in tweet
        title_length = 280 - (len(short_url) - len(BU_HASHTAG) - 6)
        title = title[:title_length] + '...'
        tweet = '{} #{} {}'.format(title, BU_HASHTAG, short_url)
    print('Tweeting: {}'.format(tweet))
    if (os.environ.get('CONSUMER_KEY_VALUE', None) and
        os.environ.get('CONSUMER_SECRET_VALUE', None) and
        os.environ.get('ACCESS_TOKEN_VALUE', None) and
            os.environ.get('ACCESS_SECRET_VALUE', None)):
        # Secrets are cached in environment variables
        print('Secrets cached')
        consumer_key = os.environ['CONSUMER_KEY_VALUE']
        consumer_secret = os.environ['CONSUMER_SECRET_VALUE']
        access_token = os.environ['ACCESS_TOKEN_VALUE']
        access_secret = os.environ['ACCESS_SECRET_VALUE']
    else:
        ssm = boto3.client('ssm')
        try:
            response = ssm.get_parameters(
                Names=[
                    CONSUMER_KEY,
                    CONSUMER_SECRET,
                    ACCESS_TOKEN,
                    ACCESS_SECRET
                ],
                WithDecryption=True
            )
        except ClientError as error:
            print('Problem getting keys from SSM: {}'.format(error))
            return
        else:
            params = response['Parameters']
            for param in params:
                if param['Name'].endswith('access.secret'):
                    access_secret = param['Value']
                    os.environ['ACCESS_SECRET_VALUE'] = access_secret
                elif param['Name'].endswith('access.token'):
                    access_token = param['Value']
                    os.environ['ACCESS_TOKEN_VALUE'] = access_token
                elif param['Name'].endswith('consumer.key'):
                    consumer_key = param['Value']
                    os.environ['CONSUMER_KEY_VALUE'] = consumer_key
                elif param['Name'].endswith('consumer.secret'):
                    consumer_secret = param['Value']
                    os.environ['CONSUMER_SECRET_VALUE'] = consumer_secret
                else:
                    print('Unknown parmaeter passed: {}'.format(param))
    try:
        auth = tweepy.OAuthHandler(consumer_key, consumer_secret)
        auth.set_access_token(access_token, access_secret)
        api = tweepy.API(auth)
        status_response = api.update_status(tweet)
    except tweepy.TweepError as error:
        print('Problem posting tweet: {}'.format(error))
        return dict(status=error, created='')
    else:
        return dict(status='OK', created=status_response._json['created_at'])


def main(event, context):
    if FEED_URL:
        d = feedparser.parse(FEED_URL)
    else:
        print('No feed URL passed')
        return
    # Create a DynamoDB client
    ddb = boto3.resource('dynamodb')
    table = ddb.Table(DYNAMODB_TABLE)
    
    # Iterate over the RSS feed
    for entry in d['entries']:
        try:
            url = entry['link']
            title = re.sub(' +', ' ', entry['title']) # Remove extra whitespace
            title = html.unescape(title) # Remove HTML elements
            query_response = table.query(
                KeyConditionExpression=Key('url').eq(url)
            )
        except KeyError:
            print('Malformed RSS returned from feed')
        except ClientError as error:
            print('Problem querying DynamoDB: {}'.format(error))
        else:
            if query_response['Count'] == 0:
                # Add event to DynamoDB table
                print('Event not found in DynamoDB')
                short_url = shorten_url(url)
                try:
                    tweet_response = tweet(title, short_url)
                    item = dict(
                        url=url,
                        short_url=short_url,
                        title=title,
                        status=tweet_response['status'],
                        created=tweet_response['created']
                    )
                    print(json.dumps(item)) # Print debug info
                    put_repsonse = table.put_item(
                        Item=item
                    )
                except ClientError as error:
                    print('Problem putting item into DynamoDB: {}'.format(error))
            else:
                # Event already in table
                pass
    return
