import pytumblr
import json
import math
import wget
import os
import requests

headers = {
	'User-Agent': 'Mozilla/5.0 (Windows; U; Windows NT 5.1; it; rv:1.8.1.11) Gecko/20071127 Firefox/2.0.0.11'
}

# Authenticate via OAuth
client = pytumblr.TumblrRestClient(
  'kGV1hMYhaHzHqOyQgqvP7CehOM7yMYqNB6T4QTPhnc9GnqmVsW', # Consumer key
  'gZprtqgS0lxNxTXVs6stGulVB0YHri4UXasPMmLfslLTUzBWJB', # Consumer secret
  'KvtuB4K3loo4JCaVfkB0dvgduMNWJx26aCo4AD9PH9H8qSkEpm', # API key
  'tDE2diSXOzyb6x35gF5f7J34I6Spj7CxiuBsb4tdDI22LanSFx'  # API secret
)

debug=False

data_directory = 'data'
data_file = 'tumblr_user_list.json'
# Make the first request
photo_urls = []
blog_names = ['blair.gemmer']
for blog_name in blog_names:
	req = client.posts(blog_name, type='photo')
	number_of_posts = req['total_posts']

	if debug:
		print(number_of_posts)
		for item in req:
			print(item)

	# Process all the blog posts as pages of posts
	offset_number = 20
	number_of_pages = int(math.ceil(number_of_posts/offset_number*1.))
	print('Grabbing ' + str(number_of_posts) + ' photos from ' + str(blog_name) + '.tumblr.com...')

	directory = os.path.join('images', 'tumblr', blog_name)
	# If folder doesn't exist, create it:
	try:
	   	os.stat(directory)
	except:
	   	os.mkdir(directory)
	contents = os.path.join(directory,'contents.txt')
	image_list = []
	image_counter = 0

	for i in range(number_of_pages+3):	
	#for i in range(1):
		if debug:
			print('Page=',i)
		req = client.posts(blog_name, type='photo', offset=i*offset_number) # to increment the offset by 20 every iteration	
		count=0
		#print json.dumps(req, indent=4)
		for post in req['posts']:			
			count+=1
			for photo in post['photos']:
				#print json.dumps(photo['original_size']['url'], indent=4)	
				url = photo['original_size']['url']
				if os.path.exists(contents):
					with open(contents, 'r') as infile:
						lines = infile.readlines()
						for line in lines:
							image_list.append(line.rstrip('\n'))
				if url not in image_list:
					print('Downloading {url}'.format(url=url))
					with open(contents, 'a+') as outfile:
						outfile.write(url+'\n')
					#wget.download(url, out=directory) # Download each file
					filename = url.split('/')[-1]
					filepath = os.path.join(directory, filename)
					response = requests.get(url, headers=headers) #, timeout=0.5)
					if response.status_code == 200:
						with open(filepath, 'wb') as outfile:
							outfile.write(response.content)

				# else:
				# 	print(url, ' exists.')
		if debug:
			print('Added ' + str(count) + ' photos.\n')
		
		
	print('Finished downloading photos.')