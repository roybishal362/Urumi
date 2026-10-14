<?php
/**
 * Registers the WooCommerce REST API key from the Kubernetes Secret.
 *
 * WooCommerce has no CLI command for creating REST keys, so this writes the
 * row it would have written itself. Like WooCommerce, it stores only an HMAC
 * of the consumer key; the consumer secret is stored as-is because it is the
 * signing key for OAuth 1.0a requests.
 */

global $wpdb;

$consumer_key    = getenv('WC_CONSUMER_KEY');
$consumer_secret = getenv('WC_CONSUMER_SECRET');
$admin           = get_user_by('login', getenv('WP_ADMIN_USER'));

if (!$consumer_key || !$consumer_secret || !$admin) {
    WP_CLI::error('WC_CONSUMER_KEY, WC_CONSUMER_SECRET and a valid WP_ADMIN_USER are required.');
}

$table = $wpdb->prefix . 'woocommerce_api_keys';
$row   = array(
    'user_id'         => $admin->ID,
    'description'     => 'woo-mcp',
    'permissions'     => 'read_write',
    'consumer_key'    => wc_api_hash($consumer_key),
    'consumer_secret' => $consumer_secret,
    'truncated_key'   => substr($consumer_key, -7),
);

$existing = $wpdb->get_var($wpdb->prepare("SELECT key_id FROM {$table} WHERE description = %s", 'woo-mcp'));

if ($existing) {
    $wpdb->update($table, $row, array('key_id' => $existing));
    WP_CLI::log('[setup] REST API key for woo-mcp is in sync');
} else {
    $wpdb->insert($table, $row);
    WP_CLI::log('[setup] REST API key for woo-mcp created');
}
