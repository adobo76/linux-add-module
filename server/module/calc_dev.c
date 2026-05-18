// SPDX-License-Identifier: GPL-2.0
/*
 * calc_dev - character device exposing +,-,*,/ on signed 64-bit integers.
 *
 * Userspace writes a `struct calc_request` to /dev/calc_dev, then read()s
 * a `struct calc_response`. Per-open state lives in file->private_data so
 * two concurrent openers each have their own pending response - no shared
 * mutable state across clients.
 */

#define pr_fmt(fmt) "calc_dev: " fmt

#include <linux/cdev.h>
#include <linux/device.h>
#include <linux/fs.h>
#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/math64.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/slab.h>
#include <linux/uaccess.h>

#include "calc_proto.h"

MODULE_LICENSE("GPL");
MODULE_AUTHOR("Aaron");
MODULE_DESCRIPTION("Calculator character device");
MODULE_VERSION("0.2");

#define DEVICE_NAME  "calc_dev"
#define CLASS_NAME   "calc"

/* Per-open session state. One of these is allocated by calc_open() and
 * stashed on filp->private_data; calc_release() frees it. */
struct calc_session {
    struct mutex lock;
    bool has_response;
    struct calc_response response;
};

static int            major;
static struct class  *calc_class;
static struct device *calc_device;
static struct cdev    calc_cdev;

/*
 * calc_compute() - perform the operation and fill out the response.
 *
 * Reads the op and operands from *req, writes result/status into *resp.
 * On a bad op or divide-by-zero, sets the matching status code and
 * leaves result at 0. Logs every computation (and refusals) to dmesg.
 *
 * Inputs:
 *   @req:  pointer to the request to evaluate (read-only).
 *   @resp: pointer to the response to populate. status, result, and
 *          _pad are all overwritten by this call.
 *
 * Returns: (void)
 */
static void calc_compute(const struct calc_request *req,
             struct calc_response *resp)
{
    resp->_pad = 0;
    resp->result = 0;

    switch (req->op) {
    case CALC_OP_ADD:
        resp->result = req->a + req->b;
        pr_info("Calculating %lld + %lld!\n", req->a, req->b);
        break;
    case CALC_OP_SUB:
        resp->result = req->a - req->b;
        pr_info("Calculating %lld - %lld!\n", req->a, req->b);
        break;
    case CALC_OP_MUL:
        resp->result = req->a * req->b;
        pr_info("Calculating %lld * %lld!\n", req->a, req->b);
        break;
    case CALC_OP_DIV:
        if (req->b == 0) {
            pr_warn("Refusing %lld / 0\n", req->a);
            resp->status = CALC_STATUS_DIV_ZERO;
            return;
        }
        resp->result = div64_s64(req->a, req->b);
        pr_info("Calculating %lld / %lld!\n", req->a, req->b);
        break;
    default:
        pr_warn("Unknown op %d\n", req->op);
        resp->status = CALC_STATUS_BAD_OP;
        return;
    }

    resp->status = CALC_STATUS_OK;
}

/*
 * calc_open() - allocate per-fd session state.
 *
 * Called by the VFS the first time userspace open()s the device.
 * Allocates a fresh struct calc_session (zero-initialized) and stashes
 * it on filp->private_data so subsequent read/write can find it again.
 *
 * Inputs:
 *   @inode: VFS inode for /dev/calc_dev (unused — we only have one device).
 *   @filp:  the new struct file just created for this open. We attach
 *           our per-fd state to filp->private_data.
 *
 * Returns:
 *   0 on success, -ENOMEM if the session allocation fails.
 */
static int calc_open(struct inode *inode, struct file *filp)
{
    // Each open call on the /dev/<device> will get its own mutex
    // and private data allocated.
    // This means only one operation can be performed at a time on the device
    // on a single file descriptor.
    //
    // GFP_KERNEL: we're in process context (running on behalf of the
    // open() syscall), so the allocator is allowed to sleep and to
    // trigger I/O while looking for free pages. From interrupt context
    // or while holding a spinlock we'd have to use GFP_ATOMIC instead.
    struct calc_session *s = kzalloc(sizeof(*s), GFP_KERNEL);
    if (!s)
        return -ENOMEM;

    mutex_init(&s->lock);
    filp->private_data = s;
    pr_info("Device opened!\n");
    return 0;
}

/*
 * calc_release() - free per-fd session state.
 *
 * Called by the VFS when the *last* reference to the struct file is
 * dropped. Note that this may not coincide with userspace's close() if
 * the fd was dup()'d or inherited across fork() — release waits until
 * all references are gone.
 *
 * Inputs:
 *   @inode: VFS inode for /dev/calc_dev (unused).
 *   @filp:  the struct file being released. filp->private_data holds
 *           the session allocated in calc_open(); we free it here.
 *
 * Returns:
 *   0 (always).
 */
static int calc_release(struct inode *inode, struct file *filp)
{
    // Free the private data allocated in calc_open.
    kfree(filp->private_data);
    pr_info("Device closed!\n");
    return 0;
}

/*
 * calc_write() - accept one calc_request and compute its response.
 *
 * The user buffer must be exactly sizeof(struct calc_request); any
 * other length returns -EINVAL. A second write before a read silently
 * overwrites the previous pending response.
 *
 * Inputs:
 *   @filp:  the open file; filp->private_data is our calc_session.
 *   @buf:   user-space pointer holding the calc_request.
 *   @count: number of bytes available in @buf; must equal
 *           sizeof(struct calc_request).
 *   @ppos:  file position (unused — the device is not seekable).
 *
 * Returns:
 *   sizeof(struct calc_request) on success;
 *   -EINVAL if @count != sizeof(struct calc_request);
 *   -EFAULT if copy_from_user() fails.
 */
static ssize_t calc_write(struct file *filp, const char __user *buf,
              size_t count, loff_t *ppos)
{
    struct calc_session *s = filp->private_data;
    struct calc_request req;

    if (count != sizeof(req))
        return -EINVAL;
    if (copy_from_user(&req, buf, sizeof(req)))
        return -EFAULT;

    mutex_lock(&s->lock);
    calc_compute(&req, &s->response);
    s->has_response = true;
    mutex_unlock(&s->lock);

    return sizeof(req);
}

/*
 * calc_read() - return the result from the previous calc_write().
 *
 * The user buffer must be at least sizeof(struct calc_response).
 * Returns -EAGAIN if no write has happened on this fd since the last
 * successful read. After a successful read the pending response is
 * cleared, so a second read without an intervening write also -EAGAINs.
 *
 * Inputs:
 *   @filp:  the open file; filp->private_data is our calc_session.
 *   @buf:   user-space destination buffer.
 *   @count: capacity of @buf in bytes; must be >= sizeof(struct calc_response).
 *   @ppos:  file position (unused — the device is not seekable).
 *
 * Returns:
 *   sizeof(struct calc_response) on success;
 *   -EINVAL if @count < sizeof(struct calc_response);
 *   -EAGAIN if no pending response is available on this fd;
 *   -EFAULT if copy_to_user() fails.
 */
static ssize_t calc_read(struct file *filp, char __user *buf,
             size_t count, loff_t *ppos)
{
    struct calc_session *s = filp->private_data;
    struct calc_response resp;

    if (count < sizeof(resp))
        return -EINVAL;

    mutex_lock(&s->lock);
    if (!s->has_response) {
        mutex_unlock(&s->lock);
        return -EAGAIN;       /* read with no pending write */
    }
    resp = s->response;
    s->has_response = false;
    mutex_unlock(&s->lock);

    if (copy_to_user(buf, &resp, sizeof(resp)))
        return -EFAULT;

    return sizeof(resp);
}

static const struct file_operations calc_fops = {
    .owner   = THIS_MODULE,
    .open    = calc_open,
    .release = calc_release,
    .read    = calc_read,
    .write   = calc_write,
};

/*
 * calc_devnode() - udev callback to set permissions on /dev/calc_dev.
 *
 * Invoked once by the kernel just before /dev/calc_dev is created.
 * We override the default 0600 to 0666 so the server (and tests) can
 * open the device without root.
 *
 * Inputs:
 *   @dev:  the device being created (unused — we only have one device).
 *   @mode: out-parameter for the permission bits. If non-NULL we set
 *          *mode = 0666.
 *
 * Returns:
 *   NULL — we don't want a custom subdirectory under /dev for this
 *   class, so we leave the default path alone.
 */
static char *calc_devnode(const struct device *dev, umode_t *mode)
{
    if (mode)
        *mode = 0666;
    return NULL;
}

/*
 * calc_dev_init() - module load: register cdev, class, and /dev node.
 *
 * Walks the four registration steps: chrdev region -> cdev -> class
 * -> device. Each step has a matching teardown in the goto cascade
 * below for the failure paths.
 *
 * Inputs: (none)
 *
 * Returns:
 *   0 on success;
 *   negative errno from whichever registration step failed.
 */
static int __init calc_dev_init(void)
{
    dev_t devno;
    int err;

    // Request a range of device numbers from the kernel.
    err = alloc_chrdev_region(&devno, 0, 1, DEVICE_NAME);
    if (err < 0) {
        pr_err("alloc_chrdev_region: %d\n", err);
        return err;
    }
    major = MAJOR(devno);

    // Register the character device with the kernel.
    cdev_init(&calc_cdev, &calc_fops);
    calc_cdev.owner = THIS_MODULE;

    // Add the character device to the kernel.
    err = cdev_add(&calc_cdev, devno, 1);
    if (err) {
        pr_err("cdev_add: %d\n", err);
        goto err_region;
    }

    // Create a class for the device so that the kernel can find it.
    calc_class = class_create(CLASS_NAME);
    if (IS_ERR(calc_class)) {
        err = PTR_ERR(calc_class);
        pr_err("class_create: %d\n", err);
        goto err_cdev;
    }

    // Set the devnode callback for the class.
    // I set to 666 because default is 0600(root only)
    calc_class->devnode = calc_devnode;

    // Now create the real device in /dev
    calc_device = device_create(calc_class, NULL, devno, NULL, DEVICE_NAME);
    if (IS_ERR(calc_device)) {
        err = PTR_ERR(calc_device);
        pr_err("device_create: %d\n", err);
        goto err_class;
    }

    pr_info("loaded (/dev/%s, major %d)\n", DEVICE_NAME, major);
    return 0;

    // If any of the above fails, clean up what we've done so far.
    // Order is important here, because we can't destroy the class if the device is still using it.
    // We can't delete the character device if the region is still allocated.
    // We can't unregister the character device region if the device is still using it.
err_class:
    class_destroy(calc_class);
err_cdev:
    cdev_del(&calc_cdev);
err_region:
    unregister_chrdev_region(devno, 1);
    return err;
}

/*
 * calc_dev_exit() - module unload: undo everything calc_dev_init() did.
 *
 * Unwinds the four registrations in the reverse order of init: device
 * destroy, class destroy, cdev remove, region release.
 *
 * Inputs: (none)
 *
 * Returns: (void)
 */
static void __exit calc_dev_exit(void)
{
    dev_t devno = MKDEV(major, 0);

    device_destroy(calc_class, devno);
    class_destroy(calc_class);
    cdev_del(&calc_cdev);
    unregister_chrdev_region(devno, 1);
    pr_info("unloaded\n");
}

module_init(calc_dev_init);
module_exit(calc_dev_exit);
