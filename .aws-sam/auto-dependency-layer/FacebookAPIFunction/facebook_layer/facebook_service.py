import json
import boto3
import requests
import time
from urllib.parse import urlparse
from datetime import datetime
from botocore.exceptions import ClientError

class FacebookService:
    def __init__(self):
        self.secrets_client = boto3.client('secretsmanager')
        self.events_client = boto3.client('events')
        self._load_secrets()

    def _load_secrets(self):
        try:
            response = self.secrets_client.get_secret_value(
                SecretId='facebook/credentials'
            )
            secrets = json.loads(response['SecretString'])
            self.app_id = secrets['app_id']
            self.app_secret = secrets['app_secret']
            self.webhook_verify_token = secrets['webhook_verify_token']
        except ClientError as e:
            raise Exception(f"Failed to load secrets: {str(e)}")

    def extract_stream_details(self, stream_url):
        """
        Extract server URL and stream key from the full stream URL as provided by Facebook Live API.
        
        :param stream_url: The full stream URL (e.g., rtmps://live-api-s.facebook.com:443/rtmp/123456789?s_bl=1&...)
        :return: Tuple of (server_url, stream_key)
        :raises ValueError: If the URL format is invalid
        """
        # Parse the URL
        parsed = urlparse(stream_url)
        
        # Extract the path and query components
        path = parsed.path
        query = parsed.query
        
        # Facebook Live Producer format requires:
        # - Server URL: rtmps://live-api-s.facebook.com:443/rtmp/
        # - Stream Key: [ID]?[query parameters]
        
        if not path.startswith('/rtmp/'):
            raise ValueError("Invalid stream URL format: missing /rtmp/ path prefix")
        
        # Extract the ID portion from the path (remove /rtmp/ prefix)
        stream_id = path[len('/rtmp/'):]
        
        # Construct the stream key by combining the ID and query parameters
        stream_key = f"{stream_id}?{query}" if query else stream_id
        
        # The server URL is the base URL with /rtmp/ path
        server_url = f"{parsed.scheme}://{parsed.netloc}/rtmp"
        
        return server_url, stream_key

    def create_live_stream(self, page_id, page_access_token, title=None, description=None):
        """
        Create a live stream on the specified Facebook page.
        
        :param page_id: The ID of the Facebook page
        :param page_access_token: Access token for the page with necessary permissions
        :param title: Optional title for the live stream
        :param description: Optional description for the live stream
        :return: Dictionary containing the server URL, stream key, and backup stream key
        """
        url = f"https://graph.facebook.com/v18.0/{page_id}/live_videos"
        params = {
            "access_token": page_access_token,
            "status": "LIVE_NOW",
            "enable_backup_ingest": True
        }
        if title:
            params["title"] = title
        if description:
            params["description"] = description
        
        try:
            response = requests.post(url, data=params)
            print(f'RAW_RESPONSE: {response}')
            if response.ok:
                data = response.json()
                if 'id' in data and 'stream_url' in data:
                    server_url, stream_key = self.extract_stream_details(data['stream_url'])
                    backup_stream_key = None
                    
                    # Handle backup stream URL, which is now in stream_secondary_urls array
                    if 'stream_secondary_urls' in data and len(data['stream_secondary_urls']) > 0:
                        _, backup_stream_key = self.extract_stream_details(data['stream_secondary_urls'][0])
                    
                    return {
                        "live_video_id": data['id'],
                        "server_url": server_url,
                        "stream_key": stream_key,
                        "backup_stream_key": backup_stream_key
                    }
                else:
                    return {"error": data.get('error', 'Unknown error')}
            else:
                return {"error": response.text}
        except Exception as e:
            return {"error": str(e)}

    def verify_webhook(self, verify_token):
            """
            Verify the webhook verification token from Meta
            
            :param verify_token: The verification token received from Meta
            :return: Boolean indicating if verification was successful
            """
            return verify_token == self.webhook_verify_token    
            
    def process_webhook_event(self, payload):
        """
        Process incoming webhook events from Meta
        
        :param payload: The JSON payload from the webhook
        :return: Processing result information and event_info for EventBridge
        """
        # Verify that this is a page webhook event
        if 'object' not in payload or payload['object'] != 'page':
            raise ValueError("Received webhook is not for a page")
        
        processed_events = []
        
        # Process each entry in the webhook
        for entry in payload.get('entry', []):
            page_id = entry.get('id')
            
            # Process each change in the entry
            for change in entry.get('changes', []):
                field = change.get('field')
                value = change.get('value', {})
                
                event_info = self._process_feed_event(value, page_id)
                if event_info:
                    processed_events.append(event_info)
        
        return processed_events

    def publish_to_eventbridge(self, event_info):
        """
        Publish event_info to EventBridge
        
        :param event_info: The event information to publish
        :return: Response from EventBridge PutEvents
        """
        try:
            response = self.events_client.put_events(
                Entries=[
                    {
                        'Source': 'facebook.webhook',
                        'DetailType': 'Facebook Webhook Event',
                        'Detail': json.dumps(event_info),
                        'EventBusName': 'default'
                    }
                ]
            )
            return response
        except Exception as e:
            print(f"Error publishing to EventBridge: {str(e)}")
            raise

    def get_user_access_token(self, auth_code, redirect_uri):
        url = "https://graph.facebook.com/v18.0/oauth/access_token"
        params = {
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "redirect_uri": redirect_uri,
            "code": auth_code
        }
        response = requests.get(url, params=params)
        return response.json()

    def extend_user_access_token(self, short_lived_token):
        url = "https://graph.facebook.com/v18.0/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "fb_exchange_token": short_lived_token
        }
        response = requests.get(url, params=params)
        return response.json()

    def extend_page_access_token(self, page_access_token):
        """
        Extends a short-lived page access token to a long-lived one (valid for about 60 days)
        
        Args:
            page_access_token (str): The short-lived page access token to extend
            
        Returns:
            dict: JSON response containing the long-lived token and expiration
        """
        url = "https://graph.facebook.com/v18.0/oauth/access_token"
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "fb_exchange_token": page_access_token,
            "access_type": "page"  # Specify that we want a page access token
        }
        response = requests.get(url, params=params)
        return response.json()    

    def get_facebook_pages(self, user_access_token):
        url = "https://graph.facebook.com/v18.0/me/accounts"
        params = {
            "fields": "id,name,access_token,category,about{full_text}, bio, description, story,fan_count,link,website,picture",
            "access_token": user_access_token
        }
        response = requests.get(url, params=params)
        data = response.json()
        
        if "data" in data:
            return data["data"]  # Returns a list of page details
        else:
            return {"error": data}

    def get_page_data(self, page_id, page_access_token): #To be deleted
        url = f"https://graph.facebook.com/v18.0/{page_id}"
        params = {
            "fields": "id,name,category,about.limit(10000),bio,description",
            "access_token": page_access_token
        }
        response = requests.get(url, params=params)
        return response.json()            

    def post_to_facebook_page(self, page_id, page_access_token, message, mediaType=None, mm_url=None):
        """
        Posts to a Facebook page based on the mediaType parameter.
        
        :param page_id: The Facebook page ID
        :param page_access_token: The access token for the page
        :param message: The text content of the post
        :param mediaType: Type of media to post ('none', 'image', or 'video')
        :param mm_url: The URL of the image (required if mediaType is 'image')
        :param mm_url: The URL of the video (required if mediaType is 'video')
        :return: JSON response from the Facebook API
        """

        print(f'MEDIA_TYPE: {mediaType}')
        
        if mediaType == 'image' and mm_url:
            url = f"https://graph.facebook.com/v18.0/{page_id}/photos"
            params = {
                "message": message,
                "url": mm_url,
                "access_token": page_access_token
            }
        elif mediaType == 'video' and mm_url:
            url = f"https://graph.facebook.com/v18.0/{page_id}/videos"
            params = {
                "description": message,
                "file_url": mm_url,
                "access_token": page_access_token
            }
        else:
            # Default to text-only post if mediaType is 'none' or not specified
            url = f"https://graph.facebook.com/v18.0/{page_id}/feed"
            params = {
                "message": message,
                "access_token": page_access_token
            }
        
        response = requests.post(url, data=params)
        return response.json()
    
    def get_page_feed(self, page_id, page_access_token, limit=25, fields=None):
        """
        Gets the feed (posts) from a Facebook page.
        
        :param page_id: The Facebook page ID
        :param page_access_token: The access token for the page
        :param limit: Number of posts to retrieve (default: 25)
        :param fields: Specific fields to retrieve (optional)
        :return: JSON response containing the page's feed
        """
        url = f"https://graph.facebook.com/v18.0/{page_id}/feed"
        
        if fields is None:
            fields = "id,message,created_time,full_picture,permalink_url,shares,reactions.summary(total_count),comments.summary(total_count)"
        
        params = {
            "access_token": page_access_token,
            "limit": limit,
            "fields": fields
        }
        
        response = requests.get(url, params=params)
        return response.json()

    def reply_to_comment(self, original_comment_id, page_access_token, reply_text, commenter_id=None):
        """
        Reply to a Facebook comment with optional commenter mention
        
        :param original_comment_id: ID of the comment to reply to
        :param page_access_token: The access token for the page
        :param reply_text: The reply message content
        :param commenter_id: ID of the commenter to mention (optional)
        :return: JSON response with status and details
        """
        print(f'COMENTER_ID: {commenter_id}')
        url = f"https://graph.facebook.com/v18.0/{original_comment_id}/comments"
        
        # Format message with @mention if commenter_id is provided
        message = reply_text
        if commenter_id:
            # Add the mention tag at the beginning of the message
            mention_tag = f"@[{commenter_id}]"
            message = f"{mention_tag} {reply_text}"
        
        params = {
            "message": message,
            "access_token": page_access_token
        }
        
        try:
            response = requests.post(url, data=params)
            response_data = response.json()
            
            # If the response contains an ID, the comment was posted successfully
            if 'id' in response_data:
                return {
                    "page_access_token": page_access_token[:15] + "..." + page_access_token[-5:],  # Truncate token for security
                    "reply_text": reply_text,
                    "mentioned_user": commenter_id if commenter_id else None,
                    "status": "success",
                    "original_comment_id": original_comment_id,
                    "reply_id": response_data.get('id'),
                    "timestamp": datetime.now().isoformat()
                }
            else:
                # Handle Facebook API error
                return {
                    "page_access_token": page_access_token[:15] + "..." + page_access_token[-5:],
                    "reply_text": reply_text,
                    "mentioned_user": commenter_id if commenter_id else None,
                    "status": "error",
                    "original_comment_id": original_comment_id,
                    "error_details": response_data.get('error', {}),
                    "timestamp": datetime.now().isoformat()
                }
        except Exception as e:
            # Handle any exceptions during the API call
            return {
                "page_access_token": page_access_token[:15] + "..." + page_access_token[-5:],
                "reply_text": reply_text,
                "mentioned_user": commenter_id if commenter_id else None,
                "status": "error",
                "original_comment_id": original_comment_id,
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def is_own_comment(self, commenter_id, page_id):
        """
        Determine if a comment was made by our own page
        
        :param commenter_id: ID of the user who made the comment
        :param page_id: ID of the page receiving the comment
        :return: Boolean indicating if the comment is from our own page
        """
        return commenter_id == page_id

    # Modified method to use the new helper methods
    def _process_feed_event(self, value, page_id):
        """
        Process feed-related webhook events with enhanced context handling
        
        :param value: The value object from the webhook change
        :param page_id: The Facebook page ID receiving the webhook
        :return: Processed event information
        """
        event_info = {
            'item': value.get('item'),
            'verb': value.get('verb')
        }
        
        if value.get('item') == 'comment' and value.get('verb') == 'add':
            # Get the commenter's ID
            commenter_id = value.get('from', {}).get('id')
            
            # Check if this comment was made by our own page/app
            if self.is_own_comment(commenter_id, page_id):
                print(f"Detected our own comment from ID: {commenter_id}. Skipping processing.")
                return None  # Skip processing our own comments
            
            # Continue with regular comment processing
            page_access_token = self._get_stored_page_token(page_id)
            
            comment_data = {
                'comment_id': value.get('comment_id'),
                'post_id': value.get('post_id'),
                'parent_id': value.get('parent_id'),
                'message': value.get('message'),
                'created_time': value.get('created_time'),
                'from': {
                    'id': commenter_id,
                    'name': value.get('from', {}).get('name')
                }
            }
            
            # Check if it's a top-level comment by comparing parent_id with post_id
            is_top_level = value.get('parent_id') == value.get('post_id')
            
            # Get thread context and prepare for AI processing
            thread_context = self._get_comment_thread_context(
                value.get('post_id'),
                value.get('comment_id'),
                value.get('parent_id'),
                is_top_level,
                page_access_token
            )
            
            event_info.update({
                'page_access_token': page_access_token,
                'comment_data': comment_data,
                'thread_context': thread_context,
                'comment_level': 'top_level' if is_top_level else 'reply',
                'owner_info' : self.get_page_data(page_id,page_access_token),
                'post_data': {
                    'id': value.get('post', {}).get('id'),
                    'status_type': value.get('post', {}).get('status_type'),
                    'is_published': value.get('post', {}).get('is_published'),
                    'updated_time': value.get('post', {}).get('updated_time'),
                    'permalink_url': value.get('post', {}).get('permalink_url')
                }
            })
            
            print(f'EVENT_INFO: {event_info}')
            return event_info
        
        return None

    def _get_comment_thread_context(self, post_id, comment_id, parent_id, is_top_level, page_access_token):
        """
        Fetch the complete context of a comment thread
        
        :param post_id: ID of the post
        :param comment_id: ID of the current comment
        :param parent_id: ID of the parent comment
        :param is_top_level: Boolean indicating if this is a top-level comment
        :param page_access_token: Access token for the page
        :return: Dictionary containing thread context
        """
        base_url = "https://graph.facebook.com/v18.0"
        
        thread_context = {
            'post_content': None,
            'comment_thread': [],
            'hierarchy': 'top_level' if is_top_level else 'reply' #Probably always 'reply'
        }
        
        try:
            # 1. Get post content
            post_response = requests.get(
                f"{base_url}/{post_id}",
                params={
                    "fields": "message,created_time",
                    "access_token": page_access_token
                }
            )
            post_data = post_response.json()
            thread_context['post_content'] = post_data.get('message', '')
            
            # 2. Get comment thread
            if not is_top_level:
                # For replies, get the parent comment and its thread
                comment_response = requests.get(
                    f"{base_url}/{parent_id}",
                    params={
                        "fields": "message,created_time,from,comments{message,created_time,from}",
                        "access_token": page_access_token
                    }
                )
                thread_data = comment_response.json()
                print(f'THREAD DATA: {thread_data}')
                thread_context['comment_thread'].append({
                    'id': parent_id,
                    'message': thread_data.get('message'),
                    'created_time': thread_data.get('created_time'),
                    'from': thread_data.get('from'),
                    'replies': thread_data.get('comments', {}).get('data', [])
                })
                print(f'CONTEXT-POST: {thread_context['post_content']}')
                print(f'CONTEXT-COMMENT: {thread_context['comment_thread']}')
            else:
                # For top-level comments, get nearby comments for context
                comments_response = requests.get(
                    f"{base_url}/{post_id}/comments",
                    params={
                        "fields": "message,created_time,from,comments.limit(5){message,created_time,from}",
                        "access_token": page_access_token,
                        "limit": 5  # Adjust based on how much context you want
                    }
                )
                thread_context['comment_thread'] = comments_response.json().get('data', [])
        
        except requests.exceptions.RequestException as e:
            print(f"Error fetching thread context: {str(e)}")
            return thread_context

        return thread_context

    def extract_page_info(self, pages_data, page_id):
        """Extract 'category' and 'about' for a given page ID"""
        page_dict = {page["id"]: page for page in pages_data}  # Convert list to dict for fast lookup

        if page_id in page_dict:
            page = page_dict[page_id]
            page_access_token = page.get("access_token", "N/A")
            extended_page_access_token = self.extend_page_access_token(page_access_token)

            print(f"EXTENDED_TOKEN: {extended_page_access_token['access_token']}")
            try:
                self._store_page_token( page_id, extended_page_access_token['access_token'])

            except Exception as e:
                print(f"Error storing token: {str(e)}")  

            return {
                "name": page.get("name", "N/A"),
                "category": page.get("category", "N/A"),
                "about": page.get("about", "N/A"),
                "access_token" : page.get("access_token", "N/A"),
                "bio": page.get("bio", "N/A"),
                "description": page.get("description", "N/A"),
                "story" : page.get("story", "N/A"),
            }
        
        return {"error": "Page ID not found"}

    def _store_page_token(self, page_id, access_token):
        """
        Store page token in your database/cache
        Also fetches and stores the page's own ID for identity verification
        """
        try:            
            # Store token, page's ID, and timestamp
            table = boto3.resource('dynamodb').Table('facebook_page_tokens')
            table.put_item(Item={
                'page_id': page_id,
                'access_token': access_token,
                'updated_at': int(time.time())
            })
        except Exception as e:
            print(f"Error storing token: {str(e)}")      

    def _get_stored_page_token(self, page_id):
        """
        Get stored page token from your database/cache
        Implement based on your storage solution
        """
        # Example implementation using AWS DynamoDB
        try:
            table = boto3.resource('dynamodb').Table('facebook_page_tokens')
            response = table.get_item(Key={'page_id': page_id})
            print(f'RESPONSE: {response}')
            if 'Item' in response:
                return response['Item']['access_token']
        except Exception as e:
            print(f"Error getting stored token: {str(e)}")
        return None            

    def get_page_subscriptions(self, page_id, page_access_token):
        """
        Get all app subscriptions for a Facebook page
        
        :param page_id: The ID of the Facebook page
        :param page_access_token: Access token for the page
        :return: JSON response containing subscription information
        """
        url = f"https://graph.facebook.com/v18.0/{page_id}/subscribed_apps"
        
        params = {
            "access_token": page_access_token
        }
        
        try:
            response = requests.get(url, params=params)
            result = response.json()
            
            # Add logging for debugging
            print(f"Get page subscriptions response: {result}")
            
            # Format the response to make it more user-friendly
            subscriptions = []
            if 'data' in result:
                for app in result['data']:
                    subscriptions.append({
                        "app_id": app.get('id'),
                        "app_name": app.get('name', 'Unknown'),
                        "subscribed_fields": app.get('subscribed_fields', [])
                    })
            
            return {
                "status": "success",
                "page_id": page_id,
                "subscriptions": subscriptions,
                "raw_response": result,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "status": "error",
                "page_id": page_id,
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def subscribe_app_to_page(self, page_id, page_access_token, fields=None):
        """
        Subscribe the app to a Facebook page to receive real-time updates
        
        :param page_id: The ID of the Facebook page
        :param page_access_token: Access token for the page
        :param fields: Comma-separated string of fields to subscribe to (default: 'feed')
        :return: JSON response from the Facebook API
        """
        if fields is None:
            fields = 'feed'
            
        url = f"https://graph.facebook.com/v18.0/{page_id}/subscribed_apps"
        
        params = {
            "access_token": page_access_token,
            "subscribed_fields": fields
        }
        
        try:
            response = requests.post(url, params=params)
            result = response.json()
            
            # Add some logging for debugging
            print(f"Subscribe app to page response: {result}")

            extended_page_access_token = self.extend_page_access_token(page_access_token)

            print(f"EXTENDED_TOKEN: {extended_page_access_token['access_token']}")

            self._store_page_token( page_id, extended_page_access_token['access_token'])
            
            return {
                "status": "success" if result.get('success') else "error",
                "page_id": page_id,
                "subscribed_fields": fields,
                "response": result,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "status": "error",
                "page_id": page_id, 
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }

    def unsubscribe_app_from_page_fields(self, page_id, page_access_token, fields_to_remove):
        """
        Unsubscribe the app from specific fields for a Facebook page
        
        :param page_id: The ID of the Facebook page
        :param page_access_token: Access token for the page
        :param fields_to_remove: String or list of fields to unsubscribe from
        :return: JSON response containing unsubscription result
        """
        url = f"https://graph.facebook.com/v18.0/{page_id}/subscribed_apps"
        
        # Convert string to list if necessary
        if isinstance(fields_to_remove, str):
            fields_to_remove = [fields_to_remove]
        
        try:
            # First, get current subscriptions
            current_subscriptions = self.get_page_subscriptions(page_id, page_access_token)
            
            if current_subscriptions["status"] == "error":
                return current_subscriptions
            
            # Get current subscribed fields from the first app (assuming it's the one we want)
            current_fields = []
            if current_subscriptions["subscriptions"]:
                current_fields = current_subscriptions["subscriptions"][0].get("subscribed_fields", [])
            
            # Remove specified fields while keeping others
            updated_fields = [field for field in current_fields if field not in fields_to_remove]
            
            if updated_fields:
                # If there are still fields left, update the subscription
                params = {
                    "access_token": page_access_token,
                    "subscribed_fields": ','.join(updated_fields)
                }
                response = requests.post(url, params=params)
            else:
                # If no fields are left, unsubscribe the app completely
                params = {"access_token": page_access_token}
                response = requests.delete(url, params=params)  # DELETE request unsubscribes the app

            result = response.json()
            
            # Add logging for debugging
            print(f"Unsubscribe fields response: {result}")
            
            return {
                "status": "success" if result.get('success') else "error",
                "page_id": page_id,
                "removed_fields": fields_to_remove,
                "remaining_fields": updated_fields,
                "response": result,
                "timestamp": datetime.now().isoformat()
            }
        except Exception as e:
            return {
                "status": "error",
                "page_id": page_id,
                "error_details": str(e),
                "timestamp": datetime.now().isoformat()
            }
