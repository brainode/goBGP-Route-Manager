from flask import current_app as app, render_template, request, redirect, url_for
from .gobgp_client import add_route_to_gobgp, delete_route_from_gobgp
from .dns_utils import resolve_domain

@app.route('/')
def index():
    # Этот маршрут будет показывать список подсетей и их next hop
    return render_template('index.html')

@app.route('/add', methods=['POST'])
def add_route():
    domain = request.form.get('domain')
    next_hop = request.form.get('next_hop')
    if domain and next_hop:
        ip_list = resolve_domain(domain)
        for ip in ip_list:
            add_route_to_gobgp(ip, next_hop)
    return redirect(url_for('index'))

@app.route('/delete', methods=['POST'])
def delete_route():
    subnet = request.form.get('subnet')
    if subnet:
        delete_route_from_gobgp(subnet)
    return redirect(url_for('index'))