import requests, json, wget, os

class InstagramClient():

	_client_id = 'a7154461155f4c5f985e66c94093046c'
	_api_url = 'https://api.instagram.com/v1/'

	def authenticate(self):
		return True

	def get_user_id(self, screen_name=None):		
		payload = {
					'q': screen_name,
					'client_id': self._client_id
		}

		response = requests.get(self._api_url+'users/search', params=payload)

		if response.status_code == 200:
			return response.json()['data'][0]['id']
		return []

	def get_user_media(self, user_id=None, num_posts=None):
		api_url = 'https://api.instagram.com/v1/users/{user_id}/media/recent'.format(user_id=user_id)
		payload = {
					'client_id': self._client_id,
					'count': num_posts
		}
		response = requests.get(api_url, params=payload)	
		if response.status_code == 200:
			responses = []
			image_urls = []
			search_result = response.json()
			responses.append(search_result)

			while search_result['pagination'] != {}:			
				next_url = search_result['pagination']['next_url']
				response = requests.get(next_url)
				search_result = response.json()
				responses.append(search_result)

			for search_result in responses:
				data = search_result['data']			
				for image in data:				
					image_url = image['images']['standard_resolution']['url']
					#print json.dumps(image_url, indent=4)
					image_urls.append(image_url)
			return image_urls
		return []

	def get_num_posts(self, user_id=None):	
		api_url = 'https://api.instagram.com/v1/users/{user_id}'.format(user_id=user_id)
		payload = {
					'client_id': self._client_id
		}
		response = requests.get(api_url, params=payload)
		if response.status_code == 200:
			results = response.json()['data']
			num_posts = results['counts']['media']
			return num_posts
		return None

	def download_media(self, url_list=None, directory=None):
		if not os.path.exists(directory):
			os.makedirs(directory)
		contents = os.path.join(directory,'contents.txt')
		image_list = []
		image_counter = 0
		if os.path.exists(contents):
			with open(contents, 'r') as infile:
				lines = infile.readlines()
				for line in lines:
					image_list.append(line.rstrip('\n'))
		for url in url_list:
			if url not in image_list:
				with open(contents, 'a+') as outfile:
					outfile.write(url+'\n')
				wget.download(url,directory)
				image_counter += 1
		print '{num_images} Images Downloaded.'.format(num_images=image_counter)

if __name__ == '__main__':
	client = InstagramClient()
	if client.authenticate():
		screen_name = 'blair.gemmer'

		user_id = client.get_user_id(screen_name=screen_name)

		num_posts = client.get_num_posts(user_id=user_id)
		print 'Posts:', num_posts
		if num_posts != None:
			url_list = client.get_user_media(user_id=user_id, num_posts=num_posts)
			#print json.dumps(url_list, indent=4)
			client.download_media(url_list=url_list, directory='images')