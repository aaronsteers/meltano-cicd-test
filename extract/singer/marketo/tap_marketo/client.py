import logging
from typing import Dict, List
import sys

import aiohttp
from fire import Fire
import pandas as pd
import requests

# Set the logging config
logging.basicConfig(stream=sys.stdout, level=logging.INFO)

class MarketoClient(object):
    def __init__(self, config: Dict[str,str]):
        self.endpoint = config.get('endpoint')
        self.identity = config.get('identity')
        self.client_id = config.get('client_id')
        self.client_secret = config.get('client_secret')
        self.start_time = config.get('start_time')
        self.access_token = self.get_access_token()
        self.initial_date_token = self.get_date_token()

    def chunker(self, full_list: List, chunk_size: int) -> List:
        """
        Generator that yields a chunk of the original list.
        """
        for i in range(0, len(full_list), chunk_size):
            yield full_list[i:i + chunk_size]

    def get_response(self, url: str, payload: Dict[str,str]):
        """
        Boilerplate for GETting a request and returning the json.
        """

        # Try to get the access token, it may not exist yet
        try:
            auth = {'access_token': self.access_token}
        except:
            auth = {'access_token': 'None'}

        params = {**auth, **payload}
        response = requests.get(url, params=params)
        if response.status_code != 200:
            logging.critical(response.status_code)
            logging.critical(response.text)
            sys.exit(1)
        else:
            return response.json()

    def check_response_success(self, response: requests.Response) -> Dict:
        """
        Marketo returns a 200 even if the request might have failed.
        Check the status in the object and return if truly successful.
        """

        if not response['success']:
            logging.critical('Request failed.')
            logging.critical(response['errors'])
            sys.exit(1)
        else:
            return response

    def get_access_token(self) -> str:
        """
        Hit the Marketo Identity endpoint to get a valid access token.
        """

        identity_url = '{}/oauth/token'.format(self.identity)
        payload = {'grant_type': 'client_credentials',
                   'client_id': self.client_id,
                   'client_secret': self.client_secret}

        return self.get_response(identity_url, payload)['access_token']

    def get_date_token(self) -> str:
        """
        Get a date-based paging token from Marketo for use in other calls.
        """

        token_url = '{}/v1/activities/pagingtoken.json'.format(self.endpoint)
        payload = {'sinceDatetime': self.start_time}
        response = self.get_response(token_url, payload)
        return response['nextPageToken']

    def get_activities(self, activity_type_ids: List[int]) -> List[Dict]:
        """
        Get a list of activities based on a datetime nextPageToken.
        """

        chunk_size = 10 # This is the limit for the API

        for type_chunk in self.chunker(activity_type_ids, chunk_size):
            activities_url = '{}/v1/activities.json'.format(self.endpoint)
            payload = {'nextPageToken': self.initial_date_token,
                       'activityTypeIds': type_chunk}

            initial_response = (self.check_response_success(
                                    self.get_response(activities_url, payload)))
            print(initial_response.keys())

            return initial_response['result']

    def get_activity_types(self) -> List[Dict]:
        """
        Get the full list of activity types.
        """

        ## TODO: Deal with the case that there are over 300 activity types
        activity_type_url = '{}/v1/activities/types.json'.format(self.endpoint)
        payload =  {}

        initial_response = (self.check_response_success(
                                self.get_response(activity_type_url, payload)))

        return initial_response['result']

    def get_leads(self):
        """
        Get lead data based on leads pulled in by the activities endpoint.
        """

        leads_url = '{}/v1/leads.json'.format(self.endpoint)
        payload = {'nextPageToken': self.initial_date_token}

        initial_response = self.get_response(leads_url, payload)


        return

    def get_data(self):
        """
        Get leads, activities and activity_types.
        """

        activity_types = self.get_activity_types()
        activity_type_ids = [record['id'] for record in activity_types]

        activities = self.get_activities(activity_type_ids)
        print(activities[0])


