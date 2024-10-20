import os
import csv
import time
from typing import List, Dict
from datetime import datetime
import getpass
import sys
from atproto import Client
from atproto.exceptions import AtProtocolError
from functools import wraps
import re

def rate_limit(calls: int, period: int):
    def decorator(func):
        calls_made = []

        @wraps(func)
        def wrapper(*args, **kwargs):
            current_time = time.time()
            
            # Remove calls older than the period
            calls_made[:] = [call_time for call_time in calls_made if current_time - call_time < period]
            
            if len(calls_made) >= calls:
                sleep_time = calls_made[0] + period - current_time
                if sleep_time > 0:
                    print(f"Rate limit reached. Sleeping for {sleep_time:.2f} seconds...")
                    time.sleep(sleep_time)
                calls_made.pop(0)
            
            calls_made.append(current_time)
            return func(*args, **kwargs)
        
        return wrapper
    return decorator

class BlueskyOrgSearch:
    def __init__(self):
        self.client = self.get_client()

    def get_client(self):
        username = os.environ.get('BSKY_USERNAME')
        password = os.environ.get('BSKY_APP_PASSWORD')
        
        if not username or not password:
            raise ValueError("Please set BKY_USERNAME and BSKY_APP_PASSWORD environment variables")
        
        client = Client()
        
        try:
            client.login(username, password)
            print("Successfully logged in!")
        except AtProtocolError as e:
            if 'AuthFactorTokenRequired' in str(e):
                print("Two-factor authentication is required.")
                while True:
                    code = getpass.getpass("Enter the 2FA code sent to your email: ")
                    try:
                        client.login(username, password, totp=code)
                        print("Successfully logged in with 2FA!")
                        break
                    except Exception as e:
                        print(f"Invalid 2FA code. Please try again. Error: {str(e)}")
            else:
                raise e
        except Exception as e:
            print(f"Login failed: {str(e)}")
            raise e
        
        return client

    @rate_limit(calls=100, period=86400)  # 1000 calls per 60 minutes
    def search_organization(self, org_name: str, org_type: str) -> List[Dict]:
        """Search for an organization and return matching accounts."""
        try:
            org_words = re.findall(r'\b\w+\b', org_name.lower())
            org_without_of = ' '.join([word for word in org_words if word != 'of'])

            matched_accounts = []
            cursor = None
            page_count = 0
            max_pages = 1  # Adjust as needed

            search_term = org_words[0] if org_words else org_name

            while True:
                try:
                    params = {'term': search_term, 'limit': 100}
                    if cursor:
                        params['cursor'] = cursor

                    results = self.client.app.bsky.actor.search_actors(params)

                    for actor in results.actors:
                        search_text = f"{actor.display_name} {actor.description or ''}".lower()

                        if (org_name.lower() in search_text or 
                            org_without_of in search_text or
                            all(word in search_text for word in org_words) or
                            self.fuzzy_match(org_name, search_text)):
                            account_info = {
                                'search_term': org_name,
                                'organization_type': org_type,
                                'handle': actor.handle,
                                'display_name': actor.display_name,
                                'description': actor.description,
                                'follower_count': getattr(actor, 'followers_count', 0),
                                'following_count': getattr(actor, 'following_count', 0),
                                'posts_count': getattr(actor, 'posts_count', 0),
                                'search_date': datetime.now().strftime('%Y-%m-%d')
                            }
                            matched_accounts.append(account_info)

                    if not results.cursor:
                        break
                    
                    cursor = results.cursor
                    page_count += 1

                    if page_count >= max_pages:
                        break

                    time.sleep(1)

                except AtProtocolError as e:
                    if 'RateLimitExceeded' in str(e):
                        reset_time = int(e.response.headers.get('RateLimit-Reset', 0))
                        wait_time = max(0, reset_time - int(time.time()))
                        print(f"Rate limit exceeded. Waiting for {wait_time} seconds...")
                        time.sleep(wait_time + 1)  # Add 1 second buffer
                        continue
                    else:
                        raise e

            return matched_accounts

        except Exception as e:
            print(f"Error searching for {org_name}: {str(e)}")
            return []

    def fuzzy_match(self, org_name: str, search_text: str) -> bool:
        """Perform a fuzzy match between the organization name and the search text."""
        org_words = org_name.lower().split()
        search_words = search_text.lower().split()

        matched_words = 0
        for org_word in org_words:
            if any(self.levenshtein_distance(org_word, search_word) <= 2 for search_word in search_words):
                matched_words += 1

        return matched_words / len(org_words) >= 0.7

    def levenshtein_distance(self, s1: str, s2: str) -> int:
        """Calculate the Levenshtein distance between two strings."""
        if len(s1) < len(s2):
            return self.levenshtein_distance(s2, s1)

        if len(s2) == 0:
            return len(s1)

        previous_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            current_row = [i + 1]
            for j, c2 in enumerate(s2):
                insertions = previous_row[j + 1] + 1
                deletions = current_row[j] + 1
                substitutions = previous_row[j] + (c1 != c2)
                current_row.append(min(insertions, deletions, substitutions))
            previous_row = current_row

        return previous_row[-1]

    def search_from_csv(self, input_file: str, output_dir: str):
        try:
            # Read organizations from CSV
            organizations = []
            with open(input_file, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                organizations = list(reader)

            # Create the output directory if it doesn't exist
            os.makedirs(output_dir, exist_ok=True)

            # Get the GITHUB_ENV file path
            github_env = os.environ.get('GITHUB_ENV')

            # Search for each organization
            total = len(organizations)
            for i, org in enumerate(organizations, 1):
                org_name = org['organization_name']
                org_type = org['type']
                print(f"Searching {i}/{total}: {org_name} ({org_type})")

                try:
                    results = self.search_organization(org_name, org_type)

                    # Write results to a separate CSV file
                    output_file = os.path.join(output_dir, f"{org_name.replace(' ', '_')}.csv")
                    with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
                        fieldnames = [
                            'search_term',
                            'organization_type',
                            'handle',
                            'display_name',
                            'description',
                            'follower_count',
                            'following_count',
                            'posts_count',
                            'search_date'
                        ]
                        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                        writer.writeheader()

                        # Write to GITHUB_ENV if it exists
                        if github_env:
                            with open(github_env, 'a') as env_file:
                                env_file.write(f"CSV_{org_name.replace(' ', '_').upper()}_HEADER={','.join(fieldnames)}\n")

                        if results:
                            for j, result in enumerate(results, 1):
                                writer.writerow(result)

                                # Write to GITHUB_ENV if it exists
                                if github_env:
                                    row_values = [str(result.get(field, '')) for field in fieldnames]
                                    env_file.write(f"CSV_{org_name.replace(' ', '_').upper()}_ROW_{j}={','.join(row_values)}\n")
                        else:
                            writer.writerow({
                                'search_term': org_name,
                                'organization_type': org_type,
                                'handle': 'NO_MATCH_FOUND',
                                'display_name': '',
                                'description': '',
                                'follower_count': 0,
                                'following_count': 0,
                                'posts_count': 0,
                                'search_date': datetime.now().strftime('%Y-%m-%d')
                            })

                        # Write the row count to GITHUB_ENV
                        if github_env:
                            env_file.write(f"CSV_{org_name.replace(' ', '_').upper()}_ROW_COUNT={len(results)}\n")

                    print(f"Results for {org_name} saved to {output_file}")

                except Exception as e:
                    print(f"Error processing {org_name}: {str(e)}")
                    # Write error to a separate CSV file
                    output_file = os.path.join(output_dir, f"{org_name.replace(' ', '_')}.csv")
                    with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
                        fieldnames = [
                            'search_term',
                            'organization_type',
                            'handle',
                            'display_name',
                            'description',
                            'follower_count',
                            'following_count',
                            'posts_count',
                            'search_date'
                        ]
                        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerow({
                            'search_term': org_name,
                            'organization_type': org_type,
                            'handle': 'ERROR',
                            'display_name': str(e),
                            'description': '',
                            'follower_count': 0,
                            'following_count': 0,
                            'posts_count': 0,
                            'search_date': datetime.now().strftime('%Y-%m-%d')
                        })
                    print(f"Error occurred for {org_name}. File created: {output_file}")

                # Add a delay between organizations to avoid hitting rate limits
                time.sleep(10)  # Wait for 10 seconds between organizations

            print(f"\nSearch completed! Results saved to {output_dir}")

        except Exception as e:
            print(f"Error processing file: {str(e)}")
            raise e


def main():
    print("Bluesky Organization Search")
    print("--------------------------")
    
    try:
        # Initialize the searcher
        searcher = BlueskyOrgSearch()
        
        # Search using the CSV file
        input_file = "test_orgs.csv"  # The CSV we created earlier
        output_dir = f"uk_research_orgs_{datetime.now().strftime('%Y-%m-%d_%H-%M')}"
        os.environ['REPORT_DIR'] = output_dir

        print(f"\nStarting search using {input_file}")
        print(f"Saving results to {output_dir}")
        print("This may take a while due to API rate limiting...")
        
        searcher.search_from_csv(input_file, output_dir)
        
    except KeyboardInterrupt:
        print("\nSearch interrupted by user. Saving progress...")
        sys.exit(1)
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
