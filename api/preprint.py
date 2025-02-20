import os
import requests
import json
from common import *
from dotenv import load_dotenv
import re
from github import Github
from github_client import gh_read_from_issue_body 
import csv
import subprocess

load_dotenv()

"""
Helper functions for the tasks 
performed by the preprint (production server).
"""

def zenodo_create_bucket(title, archive_type, creators, repository_url, issue_id):
    
    [owner,repo,provider] =  get_owner_repo_provider(repository_url,provider_full_name=True)

    # ASSUMPTION 
    # Fork exists and has the same name.
    fork_url = f"https://{provider}/roboneurolibre/{repo}"

    ZENODO_TOKEN = os.getenv('ZENODO_API')
    params = {'access_token': ZENODO_TOKEN}
    # headers = {"Content-Type": "application/json",
    #                 "Authorization": "Bearer {}".format(ZENODO_TOKEN)}
    
    # WANING: 
    # FOR NOW assuming that HEAD corresponds to the latest successful
    # book build. That may not be the case. Requires better 
    # data handling or extra functionality to retreive the latest successful
    # book commit.
    commit_user = format_commit_hash(repository_url,"HEAD")
    commit_fork = format_commit_hash(fork_url,"HEAD")

    libre_text = f"<a href=\"{fork_url}/commit/{commit_fork}\"> reference repository/commit by roboneuro</a>"
    user_text = f"<a href=\"{repository_url}/commit/{commit_user}\">latest change by the author</a>"
    review_text = f"<p>For details, please visit the corresponding <a href=\"https://github.com/neurolibre/neurolibre-reviews/issues/{issue_id}\">NeuroLibre technical screening.</a></p>"
    sign_text = "\n<p><strong><a href=\"https://neurolibre.org\" target=\"NeuroLibre\">https://neurolibre.org</a></strong></p>"

    data = {}
    data["metadata"] = {}
    data["metadata"]["title"] = title
    data["metadata"]["creators"] = creators
    data["metadata"]["keywords"] = ["canadian-open-neuroscience-platform","neurolibre"]
    # (A) NeuroLibre artifact is a part of (isPartOf) the NeuroLibre preprint (B 10.55458/NeuroLibre.issue_id)
    data["metadata"]["related_identifiers"] = [{"relation": "isPartOf","identifier": f"10.55458/neurolibre.{issue_id:05d}","resource_type": "publication-preprint"}]
    data["metadata"]["contributors"] = [{'name':'NeuroLibre, Admin', 'affiliation': 'NeuroLibre', 'type': 'ContactPerson' }]

    if (archive_type == 'book'):
        data["metadata"]["upload_type"] = "publication"
        data["metadata"]["publication_type"] = "preprint"
        data["metadata"]["description"] = f"NeuroLibre JupyterBook built at this {libre_text}, based on the {user_text}. {review_text} {sign_text}"
    elif (archive_type == 'data'):
        data["metadata"]["upload_type"] = "dataset"
        # TODO: USE OpenAI API here to explain data.
        data["metadata"]["description"] = f"Dataset provided for NeuroLibre preprint.\n Author repo: {repository_url} \nNeuroLibre fork:{fork_url} {review_text}  {sign_text}"
    elif (archive_type == 'repository'):
        data["metadata"]["upload_type"] = "software"
        data["metadata"]["description"] = f"GitHub archive of the {libre_text}, based on the {user_text}. {review_text} {sign_text}"
    elif (archive_type == 'docker'):
        data["metadata"]["upload_type"] = "software"
        data["metadata"]["description"] = f"Docker image built from the {libre_text}, based on the {user_text}, using repo2docker (through BinderHub). <br> To run locally: <ol> <li><pre><code class=\"language-bash\">docker load < DockerImage_10.55458_NeuroLibre_{issue_id:05d}_{commit_fork[0:6]}.tar.gz</code><pre></li><li><pre><code class=\"language-bash\">docker run -it --rm -p 8888:8888 DOCKER_IMAGE_ID jupyter lab --ip 0.0.0.0</code></pre> </li></ol> <p><strong>by replacing <code>DOCKER_IMAGE_ID</code> above with the respective ID of the Docker image loaded from the zip file.</strong></p> {review_text} {sign_text}"

    # Make an empty deposit to create the bucket 
    r = requests.post("https://zenodo.org/api/deposit/depositions",
                params=params,
                json=data)
    
    print(f"Error: {r.status_code} - {r.text}")
    # response_dict = json.loads(r.text)

    # for i in response_dict:
    #     print("key: ", i, "val: ", response_dict[i])

    if not r:
        return {"reason":"404: Cannot create " + archive_type + " bucket.", "commit_hash":commit_fork, "repo_url":fork_url}
    else:
        return r.json()

def zenodo_delete_bucket(remove_link):
    ZENODO_TOKEN = os.getenv('ZENODO_API')
    headers = {"Content-Type": "application/json", "Authorization": "Bearer {}".format(ZENODO_TOKEN)}
    response = requests.delete(remove_link, headers=headers)
    return response

def execute_subprocess(command):
    """
    To asynchronously execute system-levels using celery
    simple calls such as os.system will not work.

    This helper function is to issue system-level command executions 
    using celery.
    """
    # This will be called by Celery, subprocess must be handled properly
    # os.system will not work.
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # Capture the output stream
        output = process.communicate()[0]
        # Wait for the subprocess to complete and return the return code of the process
        ret = process.wait()
        if ret == 0:
            status = True
        else:
            status = False
    except subprocess.CalledProcessError as e:
        # If there's a problem with issueing the subprocess.
        output = e.output
        status = False

    return {"status": status, "message": output}

def docker_login():
    uname = os.getenv('DOCKER_USERNAME')
    pswd = os.getenv('DOCKER_PASSWORD')
    command = ["docker", "login", DOCKER_REGISTRY, "--username", uname, "--password-stdin"]
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output = process.communicate(input=pswd.encode('utf-8'))[0]
        ret = process.wait()
        if ret == 0:
            status = True
        else:
            status = False
    except subprocess.CalledProcessError as e:
        # If there's a problem with issueing the subprocess.
        output = e.output
        status = False

    return {"status": status, "message": output}

def docker_logout():
    command = ["docker", "logout", DOCKER_REGISTRY]
    result  = execute_subprocess(command)
    return result

def docker_pull(image):
    command = ["docker", "pull", image]
    result  = execute_subprocess(command)
    return result

def docker_save(image,issue_id,commit_fork):
    record_name = item_to_record_name("docker")
    save_name = os.path.join(get_archive_dir(issue_id),f"{record_name}_10.55458_NeuroLibre_{issue_id:05d}_{commit_fork[0:6]}.tar.gz")
    try:
        save_process = subprocess.Popen(['docker', 'save', image], stdout=subprocess.PIPE)
        gzip_process = subprocess.Popen(['gzip', '-c'], stdin=save_process.stdout, stdout=open(save_name, 'wb'))
        # Wait for the gzip process to complete
        ret = gzip_process.wait()
        if ret == 0:
            status = True
            output = "Success"
        else:
            status = False
            output = "Fail"
    except subprocess.CalledProcessError as e:
        # If there's a problem with issueing the subprocess.
        output = e.output
        status = False
    return {"status": status, "message": output}, save_name

def get_archive_dir(issue_id):
    path = f"/DATA/zenodo/{issue_id:05d}"
    if not os.path.exists(path):
        os.makedirs(path)
    return path

def get_deposit_dir(issue_id):
    path = f"/DATA/zenodo_records/{issue_id:05d}"
    if not os.path.exists(path):
        os.makedirs(path)
    return path
    # docker rmi $(docker images 'busybox' -a -q)

def zenodo_get_status(issue_id):

    zenodo_dir = f"/DATA/zenodo_records/{issue_id:05d}"

    # Create directory if does not exists.
    if not os.path.exists(zenodo_dir):
        os.makedirs(zenodo_dir)

    file_list = [f for f in os.listdir(zenodo_dir) if os.path.isfile(os.path.join(zenodo_dir,f))]
    res = ','.join(file_list)

    GH_BOT=os.getenv('GH_BOT')
    github_client = Github(GH_BOT)

    data_archive_exists = gh_read_from_issue_body(github_client,"neurolibre/neurolibre-reviews",issue_id,"data-archive")

    regex_repository_upload = re.compile(r"(zenodo_uploaded_repository)(.*?)(?=.json)")
    regex_data_upload = re.compile(r"(zenodo_uploaded_data)(.*?)(?=.json)")
    regex_book_upload = re.compile(r"(zenodo_uploaded_book)(.*?)(?=.json)")
    regex_docker_upload = re.compile(r"(zenodo_uploaded_docker)(.*?)(?=.json)")
    regex_deposit = re.compile(r"(zenodo_deposit)(.*?)(?=.json)")
    regex_publish = re.compile(r"(zenodo_published)(.*?)(?=.json)")
    hash_regex = re.compile(r"_(?!.*_)(.*)")

    if data_archive_exists:
        zenodo_regexs = [regex_repository_upload, regex_book_upload, regex_docker_upload]
        types = ['Repository', 'Book', 'Docker']
    else:
        zenodo_regexs = [regex_repository_upload, regex_data_upload, regex_book_upload, regex_docker_upload]
        types = ['Repository', 'Data', 'Book', 'Docker']

    rsp = []

    if not regex_deposit.search(res):
        rsp.append("<h3>Deposit</h3>:red_square: <b>Zenodo deposit records have not been created yet.</b>")
    else:
        rsp.append("<h3>Deposit</h3>:green_square: Zenodo deposit records are found.")

    rsp.append("<h3>Upload</h3><ul>")
    for cur_regex, idx in zip(zenodo_regexs, range(len(zenodo_regexs))):
        print(cur_regex)
        print(idx)
        if not cur_regex.search(res):
            rsp.append("<li>:red_circle: <b>{}</b></li>".format(types[idx] + " archive is missing"))
        else:
            tmp = cur_regex.search(res)
            json_file = tmp.string[tmp.span()[0]:tmp.span()[1]] + '.json'
            print(tmp)
            # Display file size for uploaded items, so it is informative.
            with open(os.path.join(zenodo_dir,json_file), 'r') as f:
                # Load the JSON data
                cur_record = json.load(f)
            #cur_record = json.loads(response.text)
            # Display MB or GB depending on the size.
            print(cur_record['size'])
            size = round((cur_record['size'] / 1e6),2)
            if size > 999:
                size = "{:.2f} GB".format(cur_record['size'] / 1e9)
            else:
                size = "{:.2f} MB".format(size)
            # Format
            rsp.append("<li>:green_circle: {} archive <ul><li><code>{}</code> <code>{}</code></li></ul></li>".format(types[idx], size, json_file))
    rsp.append("</ul><h3>Publish</h3>")

    if not regex_publish.search(res):
        rsp.append(":small_red_triangle_down: <b>Zenodo DOIs have not been published yet.</b>")
    else:
        rsp.append(":white_check_mark: Zenodo DOIs are published.")

    return ''.join(rsp)

def item_to_record_name(item):
    dict_map = {"data":"Dataset",
                "repository":"GitHubRepo",
                "docker":"DockerImage",
                "book":"JupyterBook"}
    if item in dict_map.keys():
        return dict_map[item]
    else: 
        return None

def zenodo_upload_book(zip_file,bucket_url,issue_id,commit_fork):
    ZENODO_TOKEN = os.getenv('ZENODO_API')
    params = {'access_token': ZENODO_TOKEN}

    with open(zip_file, "rb") as fp:
        r = requests.put(f"{bucket_url}/JupyterBook_10.55458_NeuroLibre_{issue_id:05d}_{commit_fork[0:6]}.zip",
                                params=params,
                                data=fp)

    return r

def zenodo_upload_repository(zip_file,bucket_url,issue_id,commit_fork):
    ZENODO_TOKEN = os.getenv('ZENODO_API')
    params = {'access_token': ZENODO_TOKEN}

    with open(zip_file, "rb") as fp:
        r = requests.put(f"{bucket_url}/GitHubRepo_10.55458_NeuroLibre_{issue_id:05d}_{commit_fork[0:6]}.zip",
                        params=params,
                        data=fp)
    return r

def zenodo_upload_item(upload_file,bucket_url,issue_id,commit_fork,item_name):
    ZENODO_TOKEN = os.getenv('ZENODO_API')
    params = {'access_token': ZENODO_TOKEN}
    record_name = item_to_record_name(item_name)
    extension = "zip"

    if item_name == "docker":
        extension = "tar.gz"

    if record_name:
        with open(upload_file, "rb") as fp:
            r = requests.put(f"{bucket_url}/{record_name}_10.55458_NeuroLibre_{issue_id:05d}_{commit_fork[0:6]}.{extension}",
                                    params=params,
                                    data=fp)
    else:

        r = None

    return r


def find_resource_idx(lst, repository_url):
    """
    Helper function for get_resource_lookup.
    """
    tmp = [index for index, item in enumerate(lst) if repository_url in item[0]]
    if tmp:
        return tmp[0]
    else:
        return None

def parse_tsv_content(content):
    """
    Helper function for get_resource_lookup.
    """
    # Create a CSV reader object
    reader = csv.reader(content.splitlines(), delimiter='\t')
    # Skip the header row
    next(reader)
    # Create a list to store the parsed data
    parsed_data = []
    # Iterate over each row and add it to the parsed_data list
    for row in reader:
        parsed_data.append(row)
    
    return parsed_data

def get_resource_lookup(preview_server,verify_ssl,repository_address):
    """
    For a given repository address, returns a dictionary 
    that contains the following fields:
        - "date","repository_url","docker_image","project_name","data_url","data_doi"
    IF there's a successful book build exists for the respective inquiry.

    Returns None otherwise.

    The lookup_table.tsv exists on the preview server.

    Ideally, this should be dealt with using a proper database instead of a tsv file.
    """
    
    url = f"{preview_server}/book-artifacts/lookup_table.tsv"
    headers = {'Content-Type': 'application/json'}
    API_USER = os.getenv('TEST_API_USER')
    API_PASS = os.getenv('TEST_API_PASS')
    auth = (API_USER, API_PASS)

    # Send GET request
    response = requests.get(url, headers=headers, auth=auth, verify=verify_ssl)
    
    # Process response
    if response.ok:
        # Get content body
        content = response.content.decode('utf-8')
        # Parse content
        parsed_data = parse_tsv_content(content)
        # Get string that contains the repo_url
        idx = find_resource_idx(parsed_data,repository_address)

        if idx:
            # Convert to list
            values = parsed_data[idx][0].split(",")
            # Convert to dict 
            # The last two keys are not reliable (that may contain comma that is not separating tsv column)
            # also due to subpar documentation issue with repo2data.
            keys = ["date","repository_url","docker_image","project_name","data_url","data_doi"]
            lut = dict(zip(keys, values))
        else: 
            lut = None
    else:
        
        lut = None
    
    return lut

def zenodo_publish(issue_id):
    ZENODO_TOKEN = os.getenv('ZENODO_API')
    params = {'access_token': ZENODO_TOKEN}
    # Read json record of the deposit
    message = []

    upload_status = zenodo_confirm_status(issue_id,"uploaded")

    if upload_status[1] == "no-record-found":
        return "no-record-found"

    if upload_status[0]:
        zenodo_record = get_zenodo_deposit(issue_id)
        # We need self links from each record to publish.
        for item in zenodo_record.keys():
            publish_link = zenodo_record[item]['links']['publish']
            message.append(f"\n :ice_cube: {item_to_record_name(item)} publish status:")
            r = requests.post(publish_link,params=params)
            response = r.json()
            if r.status_code==202: 
                message.append(f"\n :confetti_ball: <a href=\"{response['doi_url']}\"><img src=\"{response['links']['badge']}\"></a>")
                tmp = f"zenodo_published_{item}_NeuroLibre_{issue_id:05d}.json"
                log_file = os.path.join(get_deposit_dir(issue_id), tmp)
                with open(log_file, 'w') as outfile:
                    json.dump(r.json(), outfile)
            else:
                message.append(f"\n <details><summary> :wilted_flower: Could not publish {item_to_record_name(item)} </summary><pre><code>{r.json()}</code></pre></details>")
    else:
        message.append(f"\n :neutral_face: {upload_status[1]} all archives are uploaded for the resources listed in the deposit record. Please ask <code>roboneuro zenodo status</code> and upload the missing  archives by <code>roboneuro zenodo upload <item></code>.")

    return message

def zenodo_confirm_status(issue_id,status_type):
    """
    Helper function to confirm the uploaded or published status
    for all zenodo archive types declares in a deposit file for
    a given issue id.

    status_type can be:
        - uploaded
        - published
    """

    zenodo_record = get_zenodo_deposit(issue_id)

    if not zenodo_record:
        return [False,"no-record-found"]
    else:
        bool_array = []
        for item in zenodo_record.keys():
            if status_type == "published":
                # Does not append commit hash
                tmp = glob.glob(os.path.join(get_deposit_dir(issue_id),f"zenodo_{status_type}_{item}_NeuroLibre_{issue_id:05d}.json"))
            elif status_type == "uploaded":
                # Appends commit hash
                tmp = glob.glob(os.path.join(get_deposit_dir(issue_id),f"zenodo_{status_type}_{item}_NeuroLibre_{issue_id:05d}_*.json"))

            if tmp:
                bool_array.append(True)
            else:
                bool_array.append(False)

        all_true = all(bool_array)
        all_false = not any(bool_array)

        if all_true:
           return [True,"All"]
        elif all_false:
           return [False,"None"]
        elif not (all_true or all_false):
           return [False,"Some"]

def get_zenodo_deposit(issue_id):
    fname = f"zenodo_deposit_NeuroLibre_{issue_id:05d}.json"
    local_file = os.path.join(get_deposit_dir(issue_id), fname)
    if not os.path.exists(local_file):
        zenodo_record = None
    else:
        with open(local_file, 'r') as f:
            zenodo_record = json.load(f)
    return zenodo_record

def zenodo_collect_dois(issue_id):
    zenodo_record = get_zenodo_deposit(issue_id)
    collect = {}
    for item in zenodo_record.keys():
        tmp = glob.glob(os.path.join(get_deposit_dir(issue_id),f"zenodo_published_{item}_NeuroLibre_{issue_id:05d}_*.json"))
        collect[item] = tmp['doi_url']
    return collect