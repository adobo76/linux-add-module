// SPDX-License-Identifier: GPL-2.0
/*
 * calc_dev - barebones kernel module skeleton.
 *
 * For now it just logs to dmesg on load and unload so we can confirm the
 * insmod/rmmod cycle works end-to-end. Character-device support will be
 * added in a follow-up step.
 */

#define pr_fmt(fmt) "calc_dev: " fmt

#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/module.h>

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Aaron");
MODULE_DESCRIPTION("Calculator character device");
MODULE_VERSION("0.1");

static int __init calc_dev_init(void)
{
	pr_info("loaded\n");
	return 0;
}

static void __exit calc_dev_exit(void)
{
	pr_info("unloaded\n");
}

module_init(calc_dev_init);
module_exit(calc_dev_exit);
