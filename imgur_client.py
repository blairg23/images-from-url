import requests

class ImgurClient():

    _client_id = '7428d5cc475541b'
    # _client_secret = '455ab0ddd8cdaa3558fc0debd9084f1d5e13bf11'
    _api_url = 'https://api.imgur.com/3/';

    def authenticate(self):
        self.headers = {'Authorization': 'Client-ID ' + self._client_id}
        return True

    def search(self, search_terms):
        api_response = requests.get(self._api_url+'gallery/search/top/', headers=self.headers, params={'q': search_terms})
        search_response = api_response.json()
        results = []

        if search_response['success']:
            for search_result in search_response['data']:
                add = {
                    'id': search_result['id'],
                    'source': 'imgur',
                    'title': search_result['title'],
                    'author_url': ('http://imgur.com/user/' + search_result['account_url']) if search_result['account_url'] else '',
                    'author_image': '',
                    'author_name': search_result['account_url'],
                    'content': search_result['description'] if search_result['description'] else '',
                    'image': '',
                    'thumbnail': '',
                    'video': '',
                    'type': 'record',
                    'other': []                    
                }

                if search_result['is_album']:
                    add['url'] = 'http://imgur.com/a/' + search_result['id']
                    add['image'] = 'http://i.imgur.com/'+search_result['cover']+'.png'
                    add['thumbnail'] = 'http://i.imgur.com/'+search_result['cover']+'.png'
                else:
                    add['url'] = 'http://imgur.com/gallery/' + search_result['id']
                    add['image'] = search_result['link']

                if add['image'] == '':
                    add['type'] = 'text'

                results.append(add)
        return map(self.sanitize, results)
    
    def download_gallery(self, gallery=None, directory=None, verbose=False):
        api_response = requests.get(self._api_url+'gallery/'+gallery, headers=self.headers)
        search_response = api_response.json()
        results = []
        for image in search_response['data']['images']:
            add = {
                'title': image['id'],
                'link': image['link']
            }                
            results.append(add)
            if not os.path.exists(directory):
                os.makedirs(directory)
            with open(os.path.join(directory, 'contents.txt'), 'a+') as outfile:
                outfile.write(add['link']+'\n')
            fname,ext = os.path.splitext(add['link'])
            wget.download(add['link'], os.path.join(directory, add['title']+ext))
        if verbose:
            print json.dumps(search_response, indent=4)
            print json.dumps(results, indent=4)            
            
        return results  

if __name__ == '__main__':
    import os
    import wget
    import json

    client = ImgurClient()
    if client.authenticate():
        gallery_id = 'foqqd'
        directory = os.path.join('images', gallery_id)
        results = client.download_gallery(gallery=gallery_id, directory=directory, verbose=False)
        print len(results)
