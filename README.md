NeuroLibre web server that serves both static files and API endpoints. Please see documentation pages for [learning]() about, [deploying]() and [debugging]() this full-stack server component of NeuroLibre ecosystem.

## Learn

### Static files

Static files are the reproducible preprint content (HTML, CSS, JS, etc.) that are generated in one of the following cases:

1. The user front-end of the RoboNeuro web application (https://roboneuro.herokuapp.com/)
2. The technical screening process on the NeuroLibre review repository (https://github.com/neurolibre/neurolibre-reviews/issues)
3. The finalized version of the reproducible preprint.

Cases 1-2 are handled on the `preview` server (on [Compute Canada Arbutus](https://arbutus.cloud.computecanada.ca/) to `preview.conp.cloud`), while case 3 is handled on the `production` server (on [NeuroLibre's own cloud](https://opennebula.conp.cloud) to `preprint.conp.cloud`), both making the respective content available to the public internet.

Under the hood, we use [NGINX](https://docs.nginx.com/nginx/admin-guide/web-server/serving-static-content/) to serve static content. To manage the [DNS records](https://www.cloudflare.com/learning/dns/dns-records/) for the domain `conp.cloud` over which NGINX serves the content, we are using [Cloudflare](https://www.cloudflare.com). Cloudflare also provides [SSL/TLS encryption](https://www.cloudflare.com/learning/ssl/what-is-ssl/) and [CDN](https://www.cloudflare.com/learning/cdn/what-is-a-cdn/) (content delivery network, not Cotes-des-Neiges :), a tiny Montrealer joke).

A good understanding of these concepts is essential for successfully deploying NeuroLibre's reproducible preprints to production. Make sure you have a solid grasp of these concepts before proceeding with the deployment instructions.

### API endpoints

An application programming interface (API) endpoint is a specific location within NeuroLibre server (e.g., `preview.conp.cloud/api/books/all`) that provides access to resources and functionality that are available (e.g., list reproducible preprints on this server):

* Some of the resources and functions available on the `preview` and `production` servers differ. For instance, only the `production` server is responsible for archiving the finalized preprint content on Zenodo, while JupyterBook builds are currently only executed on the `preview` server.

* On the other hand, there are `common` resources and functions shared between the`preview` and `production` servers, such as retrieving a reproducible preprint.

There is a need to reflect this separation between `preview`, `production`, and `common` tasks in the logic of how NeuroLibre API responds to the HTTP requests. To create such a framework, we are using [Flask](https://flask.palletsprojects.com). Our Flask framework is defined by three python scripts: 

```
full-stack-server/
├─ api/
│  ├─ neurolibre_common.py
│  ├─ neurolibre_preview.py
│  ├─ neurolibre_production.py
```

Even though Flask includes a built-in web server that is suitable for development and testing, it is not designed to handle the high levels of traffic and concurrency that are typically encountered in a production environment.

[Gunicorn](https://gunicorn.org/), on the other hand, is a production-grade application server that is designed to handle large numbers of concurrent tasks. It acts as a web service gateway interface (WSGI) that knows how to talk Python to Flask. As you can infer by its name, it is an "interface" between Flask and something else that, unlike Gunicorn, knows how to handle web traffic.

That something else is a [reverse proxy server](https://docs.nginx.com/nginx/admin-guide/web-server/reverse-proxy/), and you already know its name, NGINX! It is the gatekeeper of our full-stack web server. NGINX decides whether an HTTP request is made for static content or the application logic (encapsulated by Flask, served by Gunicorn).

I know you are bored to death, so I tried to make this last bit more fun:

This Flask + Gunicorn + NGINX trio plays the music we need for a production-level NeuroLibre full-stack web server. Of these 3, NGINX and Gunicorn always have to be all ears to the requests coming from the audience. In more computer sciency terms, they need to have their own [daemons](https://en.wikipedia.org/wiki/Daemon_(computing)) 👹.

NGINX has its daemons, but we need a unix systemD (d for daemon) ritual to summon deamons upon Gunicorn 🕯👹👉🦄🕯. To do that, we need go to the `/etc/` dungeon of our ubuntu virtual machine and drop a service spell (`/systemd/neurolibre.service`). This will open a portal (a unix socket) through which Gunicorn's deamons can listen to the requests 24/7. We will tell NGINX where that socket is, so that we can guide right mortals to the right portals.

Let's finish the introductory part of our full-stack web server with reference to [this Imagine Dragons song](https://www.youtube.com/watch?v=mWRsgZuwf_8):

```
  When the kernels start to crash
  And the servers all go down
  I feel my heart begin to race
  And I start to lose my crown

  When you call my endpoint, look into systemd
  It's where my daemons hide
  It's where my daemons hide
  Don't get too close, /etc is dark inside
  It's where my daemons hide
  It's where my daemons hide

  I see the error messages flash
  I feel the bugs crawling through my skin
  I try to debug and fix the code
  But the daemons won't let me win (you need sudo)
```

P.S. No chatGPT involved here, only my demons.

### Security

On Cloudflare, we activate [full(strict)](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/full-strict/) encryption mode for handling SSL/TLS certification. In addition, we disable legacy TLS versions of  `1.0` and `1.1` due to [known vulnerabilities](https://www.acunetix.com/blog/articles/tls-vulnerabilities-attacks-final-part/). With these configurations, we receive a solid SSL Server Rating of A from [SSL Labs](https://www.ssllabs.com/ssltest/analyze.html).

While implementing SSL is a fundamental necessity for the security of our server, it is not sufficient on its own. SSL only addresses the security of the communication channel between a website and its users, and does not address other potential security vulnerabilities. For example, any web server will be subjected to brute-force attacks typically coming from large botnets. To deal with this, we are using `fail2ban`, which is a tool that monitors our nginx log files and bans IP addresses that show malicious activity, such as repeated failed login attempts.

#### What else? - Future considerations 

Another consideration is client-side certificate authorization. In this approach, clients (e.g., `roboneuro`) are required to present a digital certificate as part of the authentication process when they attempt to access a server or service. The server then verifies the certificate to determine whether the client is authorized to access the requested resource. This would require creating a client certificate on Cloudflare, then adding that to the server block :

```
ssl_client_certificate  /etc/nginx/client-ca.crt;
ssl_verify_client optional;
```

Verification must be location-optional, as it works against serving static files. To achieve this only for api endpoints, the config would look like this:

```
location /api/ {
...
if ($ssl_client_verify != "SUCCESS") { return 403; }
...
}
```

This is currently NOT implemented due to potential issues on Heroku, where our web apps are hosted. 

Alternatively, Cloudflare provides [API Shield](https://developers.cloudflare.com/api-shield/) for enterprise customers and [mutual TLS](https://developers.cloudflare.com/api-shield/security/mtls/) for anyone.

### Performance

<details>
  <summary>Expand this tab to see the list of key configurations that determine the performance of serving static files with nginx</summary>
  <ul>
    <li><code>worker_processes</code>: This directive specifies the number of worker processes that nginx should use to handle requests. By default, nginx uses one worker process, but you can increase this number if you have a multi-core system and want to take advantage of multiple cores.</li>
    <li><code>worker_connections</code>: This directive specifies the maximum number of connections that each worker process can handle. Increasing this value can improve the performance of nginx if you have a high number of concurrent connections.</li>
    <li><code>sendfile</code>: This directive enables or disables the use of the <code>sendfile()</code> system call to send file contents to clients. Enabling <code>sendfile</code> can improve the performance of nginx when serving large static files, as it allows the kernel to copy the data directly from the filesystem cache to the client without involving the worker process.</li>
    <li><code>tcp_nopush</code>: This directive enables or disables the use of the <code>TCP_NOPUSH</code> socket option, which can improve the performance of nginx when sending large responses to clients by allowing the kernel to send multiple packets in a single batch.</li>
    <li><code>tcp_nodelay</code>: This directive enables or disables the use of the <code>TCP_NODELAY</code> socket option, which can improve the performance of nginx by disabling the Nagle algorithm and allowing the kernel to send small packets as soon as they are available, rather than waiting for more data to be buffered.</li>
    <li><code>gzip</code>: This directive enables or disables gzip compression of responses. Enabling gzip compression can improve the performance of nginx by reducing the amount of data that needs to be transmitted over the network.</li>
    <li><code>etag</code>: This directive enables or disables the use of <code>ETag</code> headers in responses. Enabling <code>ETag</code> headers can improve the performance of nginx by allowing clients to cache responses and reuse them without making additional requests to the server.</li>
    <li><code>expires</code>: This directive sets the <code>Expires</code> header in responses, which tells clients to cache responses for a specified period of time. Enabling <code>Expires</code> headers can improve the performance of nginx by allowing clients to reuse cached responses without making additional requests to the server.</li>
    <li><code>keepalive_timeout</code>: This directive sets the timeout for keepalive connections, which allows clients to reuse connections for multiple requests. Increasing the value of <code>keepalive_timeout</code> can improve the performance of nginx by reducing the overhead of establishing new connections.</li>
    <li><code>open_file_cache</code>: This directive enables file caching, which can improve the performance of nginx by allowing it to reuse previously opened files rather than opening them anew for each request.</li>
  </ul>
</details>

For further details on tuning NGINX for performance, see these blog posts about [optimizing nginx configuration](https://www.nginx.com/blog/tuning-nginx/) and [load balancing](https://www.nginx.com/blog/load-balancing-with-nginx-plus).

You can use [GTMetrix](https://gtmetrix.com/) to test the loading speed of individual NeuroLibre preprints. The loading speed of these pages mainly depends on the content of the static files they contain. For example, pages with interactive plots rendered using HTML may take longer to load because they encapsulate all the data points for various UI events.

## Deploy

### Core dependencies 


### 

### Newrelic for Host and NGINX server

We will deploy New Relic Infrastructure (`newrelic-infra`) and the NGINX integration for New Relic (`nri-nginx`,[source repo](https://github.com/newrelic/nri-nginx)) to monitor the status of our host virtual machine (VM) and the NGINX server. 

With these tools, we will be able to track the performance and availability of our host and server, and identify and troubleshoot any issues that may arise. By using New Relic and the NGINX integration, we can manage and optimize the performance of our system from a single location.

> You need credentials to login to [NewRelic portal](https://one.newrelic.com/). Otherwise you cannot proceed with the installation and monitoring. 

Ssh into the VM (`ssh -i ~/.ssh/your_key root@full-stack-server-ip-address`) and follow these instructions:

1. Install new relic infrastructure agent 

After logging into the newrelic portal, click `+ add data`, then type `ubuntu` in the search box. Under the `infrastructure & OS`, click `Linux`. When you click the `Begin installation` button, the installation command with proper credentials will be generated. Simply copy/paste and execute that command on the VM terminal.  

Alternatively, you can replace `<NEWRELIC-API-KEY-HERE>` and `<NEWRELIC-ACCOUNT-ID-HERE>` with the respective content below (please do not include the angle brackets).

```bash
curl -Ls https://download.newrelic.com/install/newrelic-cli/scripts/install.sh | bash && sudo NEW_RELIC_API_KEY=<NEWRELIC-API-KEY-HERE> NEW_RELIC_ACCOUNT_ID=<NEWRELIC-ACCOUNT-ID-HERE> /usr/local/bin/newrelic install
```

After successful installation, the newrelic agent should start running. Confirm its status by:

```bash
sudo systemctl status newrelic-infra.service
```

2. Install new relic nginx integration

* Download the `nri-nginx_*_amd64.deb` from the assets of the latest (or a desired) [nri-nginx release](https://github.com/newrelic/nri-nginx/releases). You can get the download link by right clicking the respective release asset:

```
wget https://github.com/newrelic/nri-nginx/releases/download/v3.2.5/nri-nginx_3.2.5-1_amd64.deb -O ~/nri-nginx_amd64.deb
```

* Install the package

```
cd ~
sudo apt install ./nri-nginx_amd64.deb
```

If the installation is successful, you should see `nginx-config.yaml.sample` upon: 

```
ls /etc/newrelic-infra/integrations.d
```

For the next step, confirm that the `stab_status` of the nginx is properly exposed to `127.0.0.1/status` by:

```bash
curl 127.0.0.1/status
```

The output should look like:

```
Active connections: 1 
server accepts handled requests
 126 126 125 
Reading: 0 Writing: 1 Waiting: 0 
```

3. Configure the nginx agent

We will use the default configuration provided in the sample configuration by copying it to a new file:

```
cd /etc/newrelic-infra/integrations.d
sudo cp nginx-config.yml.sample nginx-config.yml
```

This action will start the `nri-nginx` integration. Run `sudo systemctl status newrelic-infra.service` to confirm successful. You should see the _"Integration health check finished with success"_ message for _integration_name=nri-nginx_.

## Monitor, Debug, and Improve

 

4. Monitor the NGINX server 

Login to the NewRelic portal and click `All entities`. If the integration is successful, a new entity will appear under the `NGINX servers` (something like `server:localhost.localdomain:80`). 